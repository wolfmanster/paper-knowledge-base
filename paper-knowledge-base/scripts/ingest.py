"""
论文知识库 — 导入流水线
========================
遍历 Papers/ 目录，为每篇论文执行：
  PDF 提取 → 分块 → 嵌入 → 存储到 ChromaDB
"""

import time

from index_generation import mark_index_changed
from tqdm import tqdm
from models import get_or_create_chroma_collection, load_bi_encoder
from paths import CHROMA_DIR, CHUNK_MAX_WORDS, SCRIPTS_DIR
from text_processing import (
    chunk_text,
    compute_paper_id,
    extract_text_from_pdf,
    extract_title_from_filename,
    is_supported_file,
)
from utils import setup_logging

# ── 路径（使用绝对路径）────────────────────────────────────────

PAPERS_DIR = SCRIPTS_DIR.parent / "Papers"
LOG_FILE = CHROMA_DIR.parent / "ingest.log"

# ── 日志 ─────────────────────────────────────────────────────

logger = setup_logging("ingest", LOG_FILE, mode="w")

# ── 嵌入模型 ─────────────────────────────────────────────────

EMBED_BATCH_SIZE = 32


def load_existing_paper_ids(collection) -> set:
    """分页加载已处理的 paper_id，避免一次性加载过多。"""
    existing_ids: set[str] = set()
    offset = 0
    batch = 1000
    try:
        while True:
            result = collection.get(include=[], limit=batch, offset=offset)
            if not result["ids"]:
                break
            for mid in result["ids"]:
                existing_ids.add(mid.split("#")[0])
            offset += batch
    except Exception as e:
        logger.warning("读取已有记录时出错: %s", e)
    return existing_ids


# ── 主流程 ───────────────────────────────────────────────────

def main():
    PAPERS_DIR.mkdir(exist_ok=True)

    model = load_bi_encoder()

    logger.info("连接向量数据库")
    collection = get_or_create_chroma_collection()

    existing_ids = load_existing_paper_ids(collection)
    logger.info("数据库中已有 %d 篇论文\n", len(existing_ids))

    pdf_files = sorted([f for f in PAPERS_DIR.iterdir() if is_supported_file(f)])
    logger.info("共发现 %d 个文件待处理\n", len(pdf_files))

    skipped = 0
    new_count = 0
    total_chunks = 0
    errors = []
    start_time = time.time()

    for pdf_path in tqdm(pdf_files, desc="处理论文", unit="篇"):
        paper_id = compute_paper_id(pdf_path)
        if paper_id in existing_ids:
            skipped += 1
            continue

        title = extract_title_from_filename(pdf_path)

        # 1. 提取文本
        raw_text = extract_text_from_pdf(pdf_path)
        if not raw_text or len(raw_text.strip()) < 50:
            logger.debug("  跳过（文本不足）: %s", pdf_path.name)
            skipped += 1
            continue

        # 2. 分块
        chunks = chunk_text(raw_text, title=title, max_words=CHUNK_MAX_WORDS)
        if not chunks:
            logger.debug("  跳过（无有效块）: %s", pdf_path.name)
            skipped += 1
            continue

        # 3. 构建 ChromaDB 记录
        ids = [f"{paper_id}#{c['chunk_index']}" for c in chunks]
        documents = [c["text"] for c in chunks]
        metadatas = [
            {
                "paper_id": paper_id,
                "title": title,
                "filename": pdf_path.name,
                "section": c.get("section", ""),
                "chunk_index": c["chunk_index"],
                "total_chunks": len(chunks),
            }
            for c in chunks
        ]

        # 4. 生成嵌入向量
        try:
            embeddings = model.encode(
                documents,
                batch_size=EMBED_BATCH_SIZE,
                normalize_embeddings=True,
                show_progress_bar=False,
            )
        except Exception as e:
            logger.warning("  嵌入失败: %s — %s", pdf_path.name, e)
            errors.append((pdf_path.name, str(e)))
            skipped += 1
            continue

        # 5. 写入 ChromaDB（分批，每批 50 个 chunk）
        try:
            # 先清除该论文可能残留的旧 chunks（来自之前失败的回滚）
            try:
                old_ids = collection.get(
                    include=[],
                    where={"paper_id": paper_id},
                )["ids"]
                if old_ids:
                    collection.delete(ids=old_ids)
                    logger.debug("  清理旧 chunks: %d 个", len(old_ids))
            except Exception:
                pass

            batch_size = 50
            inserted_ids = []
            for i in range(0, len(ids), batch_size):
                batch_ids = ids[i : i + batch_size]
                collection.add(
                    ids=batch_ids,
                    embeddings=embeddings[i : i + batch_size].tolist(),
                    documents=documents[i : i + batch_size],
                    metadatas=metadatas[i : i + batch_size],
                )
                inserted_ids.extend(batch_ids)
        except Exception as e:
            logger.warning("  写入数据库失败: %s — %s", pdf_path.name, e)
            # 回滚已插入的 chunks，确保下次重试能重新导入
            try:
                collection.delete(ids=inserted_ids)
                logger.info("  已回滚 %d 个 chunks: %s", len(inserted_ids), pdf_path.name)
            except Exception as rollback_err:
                logger.warning("  回滚失败（需手动清理）: %s", rollback_err)
                logger.warning("  已在 existing_ids 中移除 paper_id=%s，下次重跑将强制重试", paper_id)
                existing_ids.discard(paper_id)  # 确保下次重试该论文
            # 回滚失败也好、成功也好，都不记录 paper_id，确保下次重试
            errors.append((pdf_path.name, str(e)))
            skipped += 1
            continue

        # 处理成功，记录 paper_id
        existing_ids.add(paper_id)
        new_count += 1
        total_chunks += len(ids)
        mark_index_changed()

        if new_count % 10 == 0:
            elapsed = time.time() - start_time
            rate = new_count / elapsed if elapsed > 0 else 0
            logger.info(
                "  进度: %d/%d 篇, %d chunks, 速率: %.1f 篇/分",
                new_count,
                len(pdf_files) - len(existing_ids) + new_count,
                total_chunks,
                rate * 60,
            )

    elapsed = time.time() - start_time
    logger.info("\n" + "=" * 50)
    logger.info("处理完成!")
    logger.info("  新增论文: %d 篇", new_count)
    logger.info("  跳过的论文: %d 篇", skipped)
    logger.info("  总 Chunks: %d", total_chunks)
    logger.info("  数据库总数: %d", collection.count())
    logger.info("  耗时: %.1f 秒 (%.1f 分钟)", elapsed, elapsed / 60)

    if errors:
        logger.info("\n错误列表 (%d 个):", len(errors))
        for name, err in errors[:10]:
            logger.info("  - %s: %s", name, err)
        if len(errors) > 10:
            logger.info("  ... 还有 %d 个错误", len(errors) - 10)

    logger.info("=" * 50)

    # 如果有新论文导入，自动重建文本搜索索引
    if new_count > 0:
        logger.info("\n检测到 %d 篇新论文，自动重建文本搜索索引...", new_count)
        try:
            from build_index import main as build_index_main
            build_index_main()
        except Exception as e:
            logger.warning("文本索引重建失败（语义搜索不受影响）: %s", e)
            logger.info("可以稍后手动运行: python scripts/build_index.py")


if __name__ == "__main__":
    main()
