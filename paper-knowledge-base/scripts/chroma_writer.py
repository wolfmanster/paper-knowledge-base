"""ChromaDB 写入、去重与删除同步。

从 sync_zotero.py 拆分而来。包含 Bi-Encoder 加载、集合获取、两层去重、
带事务回滚的 chunks 替换、以及 Zotero 删除项的同步清理。

延迟导入 chromadb / sentence_transformers / index_generation，
确保模块本身可在缺少这些依赖的环境下被导入（例如纯文本搜索路径）。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

COLLECTION_NAME = "papers"


def load_bi_encoder():
    """加载 SentenceTransformer Bi-Encoder 模型。"""
    from sentence_transformers import SentenceTransformer

    model_path = (
        Path.home()
        / ".cache"
        / "huggingface"
        / "hub"
        / "models--sentence-transformers--paraphrase-multilingual-MiniLM-L12-v2"
        / "snapshots"
        / "e8f8c211226b894fcb81acc59f3b34ba3efd5f42"
    )

    try:
        if model_path.exists():
            logger.info("从本地缓存加载 Bi-Encoder")
            return SentenceTransformer(str(model_path))
        logger.info("从 HuggingFace 下载 Bi-Encoder")
        return SentenceTransformer(
            "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
        )
    except Exception as e:
        logger.error("Bi-Encoder 加载失败: %s", e)
        sys.exit(1)


def get_or_create_collection(client):
    """获取或创建 ChromaDB 的 papers 集合。"""
    import chromadb
    try:
        return client.get_collection(COLLECTION_NAME)
    except (ValueError, chromadb.errors.NotFoundError):
        return client.create_collection(
            name=COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
        )


def deduplicate_paper(doi: str, title: str, collection) -> str | None:
    """检查论文是否已在 ChromaDB 中。

    两层去重策略:
    1. DOI 精确匹配（优先）
    2. 规范化标题精确匹配（回退）

    Args:
        doi: DOI 字符串（可能为空）
        title: 论文标题
        collection: ChromaDB collection

    Returns:
        已有的 paper_id（匹配到重复），None（新论文）
    """
    # 第一层：DOI 精确匹配
    if doi:
        norm_doi = doi.strip().lower().rstrip(".")
        try:
            result = collection.get(
                include=["metadatas"],
                where={"doi": norm_doi},
                limit=1,
            )
            if result["ids"]:
                matched_id = result["metadatas"][0].get("paper_id", "")
                logger.debug("  DOI 匹配成功: %s -> %s", norm_doi, matched_id)
                return matched_id
        except Exception as e:
            logger.debug("  DOI 查询异常: %s", e)

    # 第二层：规范化标题精确匹配。Chroma metadata 的 $contains 对字符串
    # 不是子串查询，因此分页读取元数据后在 Python 中比较。
    if title:
        title_norm = _normalize_title(title)
        offset = 0
        batch_size = 1000
        try:
            while True:
                result = collection.get(
                    include=["metadatas"],
                    limit=batch_size,
                    offset=offset,
                )
                ids = result.get("ids") or []
                metadatas = result.get("metadatas") or []
                for doc_id, metadata in zip(ids, metadatas):
                    if metadata and _normalize_title(metadata.get("title", "")) == title_norm:
                        matched_id = metadata.get("paper_id", doc_id.split("#")[0])
                        logger.debug("  标题匹配成功: '%s' -> %s", title_norm, matched_id)
                        return matched_id
                if len(ids) < batch_size:
                    break
                offset += len(ids)
        except Exception as e:
            logger.debug("  标题查询异常: %s", e)

    return None


def _normalize_title(title: str) -> str:
    """Normalize a title for conservative duplicate detection."""
    import re

    return " ".join(re.sub(r"[^\w\s]", " ", title, flags=re.UNICODE).casefold().split())


def _get_existing_zotero_item_id(collection, paper_id: str) -> int | None:
    """Return the Zotero item id stored for an existing paper, if any."""
    try:
        result = collection.get(
            include=["metadatas"],
            where={"paper_id": paper_id},
            limit=1,
        )
        metadatas = result.get("metadatas") or []
        if metadatas:
            item_id = metadatas[0].get("zotero_item_id")
            return int(item_id) if item_id is not None else None
    except (TypeError, ValueError) as e:
        logger.debug("  已有 Zotero itemID 无效: %s", e)
    except Exception as e:
        logger.debug("  查询已有 Zotero itemID 失败: %s", e)
    return None


def is_duplicate_zotero_item(
    existing_zotero_item_id: int | None,
    zotero_item_id: int,
) -> bool:
    """Whether a matched paper belongs to a different Zotero item.

    A full rescan refreshes the source item already represented in the vector
    store, but must not make a second Zotero entry for the same paper eligible
    for extraction and indexing.
    """
    return existing_zotero_item_id != zotero_item_id


def replace_paper_chunks(
    collection,
    *,
    paper_id: str,
    ids: list[str],
    embeddings,
    documents: list[str],
    metadatas: list[dict],
    batch_size: int = 50,
) -> None:
    """Replace one paper while restoring its previous chunks on failure."""
    old = collection.get(
        include=["documents", "metadatas", "embeddings"],
        where={"paper_id": paper_id},
    )
    old_ids = old.get("ids") or []
    old_documents = old.get("documents") or []
    old_metadatas = old.get("metadatas") or []
    old_embeddings = old.get("embeddings")
    if hasattr(old_embeddings, "tolist"):
        old_embeddings = old_embeddings.tolist()
    old_embeddings = old_embeddings or []

    inserted_ids: list[str] = []
    try:
        if old_ids:
            collection.delete(ids=old_ids)

        for start in range(0, len(ids), batch_size):
            end = start + batch_size
            batch_embeddings = embeddings[start:end]
            if hasattr(batch_embeddings, "tolist"):
                batch_embeddings = batch_embeddings.tolist()
            collection.add(
                ids=ids[start:end],
                embeddings=batch_embeddings,
                documents=documents[start:end],
                metadatas=metadatas[start:end],
            )
            inserted_ids.extend(ids[start:end])
    except Exception:
        if inserted_ids:
            try:
                collection.delete(ids=inserted_ids)
            except Exception as cleanup_error:
                logger.error("  清理失败的新 chunks 时出错: %s", cleanup_error)

        if old_ids:
            try:
                for start in range(0, len(old_ids), batch_size):
                    end = start + batch_size
                    collection.add(
                        ids=old_ids[start:end],
                        embeddings=old_embeddings[start:end],
                        documents=old_documents[start:end],
                        metadatas=old_metadatas[start:end],
                    )
                logger.info("  已恢复 %d 个旧 chunks", len(old_ids))
            except Exception as restore_error:
                logger.critical("  旧 chunks 恢复失败，需要人工修复: %s", restore_error)
        raise
    else:
        from index_generation import mark_index_changed

        mark_index_changed()


# ═══════════════════════════════════════════════════════════════
#  删除同步
# ═══════════════════════════════════════════════════════════════

def cleanup_deleted_items(cursor, collection, since_version: int = 0) -> int:
    """清理 ChromaDB 中已在 Zotero 中被删除的论文。

    Args:
        since_version: 只检查此版本之后的删除操作

    Returns:
        清理的论文数量
    """
    deleted_ids = cursor.execute(
        "SELECT itemID FROM deletedItems WHERE dateDeleted IS NOT NULL"
    ).fetchall()

    removed = 0
    for row in deleted_ids:
        item_id = row["itemID"]
        # 在 ChromaDB 中查找匹配 zotero_item_id 的 chunks
        result = collection.get(
            include=[],
            where={"zotero_item_id": item_id},
        )
        if result["ids"]:
            try:
                # 获取唯一的 paper_ids
                meta_result = collection.get(
                    include=["metadatas"],
                    where={"zotero_item_id": item_id},
                )
                paper_ids = set()
                for m in meta_result["metadatas"]:
                    paper_ids.add(m.get("paper_id", ""))

                # 删除所有匹配的 chunks
                collection.delete(ids=result["ids"])
                for pid in paper_ids:
                    logger.info("  已删除 Zotero 中移除的论文: paper_id=%s", pid)
                removed += len(paper_ids)
            except Exception as e:
                logger.warning("  删除失败 (itemID=%d): %s", item_id, e)

    if removed:
        from index_generation import mark_index_changed

        mark_index_changed()
    return removed
