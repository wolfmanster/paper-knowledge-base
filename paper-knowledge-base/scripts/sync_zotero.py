"""
Zotero → 论文知识库 同步脚本
============================
从 Zotero 的本地 SQLite 数据库中读取论文元数据和 PDF 附件，
通过 MinerU 提取文本，分块嵌入后写入 ChromaDB。

用法:
  python scripts/sync_zotero.py                   # 全量/增量同步
  python scripts/sync_zotero.py --dry-run         # 试运行
  python scripts/sync_zotero.py --full-rescan     # 强制全量重建
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
import time
from pathlib import Path

from models import get_or_create_chroma_collection, load_bi_encoder
from paths import CHROMA_DIR, SCRIPTS_DIR
from utils import ensure_utf8_stdout, get_version, setup_logging

# 子模块导入：以裸名引入命名空间，便于测试通过 monkeypatch 替换
from chroma_ops import (  # noqa: E402
    _get_existing_zotero_item_id,
    cleanup_deleted_items,
    deduplicate_paper,
    is_duplicate_zotero_item,
    replace_paper_chunks,
)
from mineru_runner import (  # noqa: E402
    DEFAULT_MINERU_TIMEOUT_SECONDS,
    MINERU_SCRIPT,
    configure as configure_mineru,
    extract_with_mineru,
    resolve_attachment_path,
    resolve_mineru_python,
    run_captured_process,
)
from sync_lock import SyncProcessLock  # noqa: E402
from zotero_db import (  # noqa: E402
    check_zotero_version,
    format_authors,
    get_attachment_info,
    get_paper_items,
    get_zotero_db_connection,
    lookup_collections,
)

# ── 路径 ─────────────────────────────────────────────────────
REPO_ROOT = SCRIPTS_DIR.parent

CHECKPOINT_FILE = REPO_ROOT / "kb" / "zotero_checkpoint.json"
LOG_FILE = REPO_ROOT / "kb" / "sync_zotero.log"
SYNC_LOCK_FILE = REPO_ROOT / "kb" / "sync_zotero.lock"

# Zotero 默认路径
DEFAULT_ZOTERO_DIR = Path.home() / "Zotero"
# MinerU 默认回退：尝试 ../MinerU-GUI（monorepo 兄弟目录）
_DEFAULT_MINERU_DIR = (SCRIPTS_DIR.parent.parent / "MinerU-GUI").resolve()


# ── 日志 ─────────────────────────────────────────────────────
CHROMA_DIR.mkdir(parents=True, exist_ok=True)

ensure_utf8_stdout()
logger = setup_logging("sync_zotero", LOG_FILE)


# ═══════════════════════════════════════════════════════════════
#  检查点管理
# ═══════════════════════════════════════════════════════════════

def load_checkpoint() -> dict:
    """从检查点文件加载上次同步状态。

    Returns:
        {"last_item_id": int, "last_version": int, "pending_item_ids": list[int]}
    """
    default = {"last_item_id": 0, "last_version": 0, "pending_item_ids": []}
    if not CHECKPOINT_FILE.exists():
        return default

    try:
        data = json.loads(CHECKPOINT_FILE.read_text(encoding="utf-8"))
        pending = data.get("pending_item_ids", [])
        if not isinstance(pending, list):
            pending = []
        return {
            "last_item_id": data.get("last_item_id", 0),
            "last_version": data.get("last_version", 0),
            "pending_item_ids": [int(item_id) for item_id in pending],
        }
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError, KeyError) as e:
        logger.warning("检查点文件损坏 (%s)，将重新全量同步", e)
        return default


def save_checkpoint(state: dict):
    """原子写入检查点文件。"""
    tmp = CHECKPOINT_FILE.with_suffix(".tmp")
    tmp.write_text(
        json.dumps({**state, "last_sync_time": time.strftime("%Y-%m-%dT%H:%M:%S")},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(CHECKPOINT_FILE)


def compute_next_last_item_id(previous_item_id: int, items: list[dict]) -> int:
    """Advance the item checkpoint without ever moving it backwards."""
    return max(
        previous_item_id,
        max((int(item["item_id"]) for item in items), default=previous_item_id),
    )


# ═══════════════════════════════════════════════════════════════
#  主流程
# ═══════════════════════════════════════════════════════════════

def _parse_args() -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(description="从 Zotero 同步论文到知识库")
    parser.add_argument("--version", action="store_true",
                        help="显示版本号并退出")
    parser.add_argument("--zotero-dir", type=Path, default=DEFAULT_ZOTERO_DIR,
                        help="Zotero 数据目录（默认: ~/Zotero）")
    parser.add_argument("--mineru-dir", type=Path, default=None,
                        help="MinerU GUI 目录（默认: 无，需通过 $MINERU_DIR 环境变量或此参数指定）")
    parser.add_argument("--full-rescan", action="store_true",
                        help="忽略检查点，强制全量重建")
    parser.add_argument("--dry-run", action="store_true",
                        help="仅显示操作，不实际写入")
    parser.add_argument("--skip-build-index", action="store_true",
                        help="同步后不重建 FTS 文本索引")
    parser.add_argument("--mineru-timeout-hours", type=float, default=24.0,
                        help="单篇 MinerU 最长运行小时数（默认: 24）")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="详细日志输出")
    args = parser.parse_args()

    if args.mineru_timeout_hours <= 0:
        parser.error("--mineru-timeout-hours 必须大于 0")

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    return args


def _resolve_mineru_paths(args: argparse.Namespace) -> None:
    """解析 MinerU 路径并配置子模块。

    优先级：CLI > 环境变量 > ../MinerU-GUI（monorepo 回退）
    """
    if args.mineru_dir is not None:
        mineru_dir: Path | None = args.mineru_dir.resolve()
    else:
        env_dir = os.environ.get("MINERU_DIR", "").strip()
        mineru_dir = (
            Path(env_dir).resolve() if env_dir
            else (_DEFAULT_MINERU_DIR if _DEFAULT_MINERU_DIR.is_dir() else None)
        )

    mineru_python = resolve_mineru_python(mineru_dir) if mineru_dir else None
    configure_mineru(mineru_dir, mineru_python)

    if mineru_python:
        logger.info("  MinerU: %s", mineru_python)
    else:
        logger.info("  MinerU: 未配置（PDF 提取将跳过）")


def _load_collection_map(cursor) -> dict[int, dict]:
    """加载 Zotero collections 表到内存（N+1 查询优化）。"""
    logger.info("加载 Zotero 分类结构...")
    collection_map: dict[int, dict] = {}
    for row in cursor.execute(
        "SELECT collectionID, collectionName, parentCollectionID FROM collections"
    ):
        collection_map[row["collectionID"]] = {
            "name": row["collectionName"],
            "parent": row["parentCollectionID"],
        }
    logger.debug("  加载了 %d 个集合", len(collection_map))
    return collection_map


def _init_stats(total: int) -> dict:
    return {
        "total": total,
        "skipped_no_attachment": 0,
        "skipped_dup": 0,
        "skipped_extract_fail": 0,
        "imported": 0,
        "errors": [],
    }


def _compute_paper_id(item: dict) -> str:
    """根据 DOI 或 Zotero key 计算确定性的 paper_id。"""
    if item["doi"]:
        from text_processing import compute_paper_id_from_doi
        return compute_paper_id_from_doi(item["doi"])

    raw = f"zotero:{item['key']}:{item['item_id']}".encode()
    paper_id = hashlib.sha256(raw).hexdigest()[:12]
    logger.debug("  无 DOI，使用 zotero key 计算 paper_id: %s", paper_id)
    return paper_id


def _process_single_item(
    item: dict,
    *,
    cursor,
    collection,
    model,
    collection_map: dict[int, dict],
    zotero_storage: Path,
    mineru_output_dir: Path,
    dry_run: bool,
    mineru_timeout_hours: float,
    stats: dict,
    pending_item_ids: set[int],
) -> None:
    """处理单篇论文：去重 → 提取附件 → MinerU → 分块 → 嵌入 → 写入 ChromaDB。"""
    item_id = item["item_id"]
    title_display = item["title"][:60] if item["title"] else "(无标题)"

    logger.info("[%d/%d] 处理: %s", stats["imported"] + stats["skipped_no_attachment"]
                + stats["skipped_dup"] + stats["skipped_extract_fail"] + 1,
                stats["total"], title_display)
    logger.debug("  itemID=%d, key=%s, DOI=%s", item_id, item["key"],
                 item["doi"][:60] if item["doi"] else "N/A")

    # 1. 计算 paper_id
    paper_id = _compute_paper_id(item)

    # 2. 去重检查
    existing_id = deduplicate_paper(item["doi"], item["title"], collection)
    if existing_id:
        existing_zotero_item_id = _get_existing_zotero_item_id(collection, existing_id)
        if is_duplicate_zotero_item(existing_zotero_item_id, item_id):
            logger.info(
                "  -> 跳过重复 Zotero 条目: 已由 itemID=%s 导入 (paper_id=%s)",
                existing_zotero_item_id if existing_zotero_item_id is not None else "未知来源",
                existing_id,
            )
            stats["skipped_dup"] += 1
            return
        # 已同步 Zotero 项的版本变化应覆盖原记录；全量重扫也沿用已有 ID。
        paper_id = existing_id
        logger.debug("  更新已有论文: paper_id=%s", paper_id)

    # 3. 获取附件
    attachment = get_attachment_info(cursor, item_id)
    if not attachment:
        logger.info("  -> 跳过: 无 PDF 或 Word 附件")
        stats["skipped_no_attachment"] += 1
        pending_item_ids.add(item_id)
        return

    att_key = attachment["key"]
    content_type = attachment["content_type"]
    att_path = resolve_attachment_path(zotero_storage, att_key, content_type)
    if not att_path:
        logger.info("  -> 跳过: storage 中未找到附件 (key=%s, type=%s)", att_key, content_type)
        stats["skipped_no_attachment"] += 1
        pending_item_ids.add(item_id)
        return

    type_label = "Word" if content_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document" else "PDF"
    logger.info("  %s: %s", type_label, att_path.name)

    if dry_run:
        logger.info("  [dry-run] 将提取并导入此论文")
        return

    # 4. MinerU 提取文本
    mineru_result = extract_with_mineru(
        att_path,
        mineru_output_dir,
        timeout_seconds=mineru_timeout_hours * 3600,
    )
    if not mineru_result:
        logger.info("  -> 跳过: MinerU 提取失败")
        stats["skipped_extract_fail"] += 1
        pending_item_ids.add(item_id)
        return

    doc_text = mineru_result.get("text", "")
    if not doc_text or len(doc_text.strip()) < 20:
        logger.info("  -> 跳过: 提取文本过短")
        stats["skipped_extract_fail"] += 1
        pending_item_ids.add(item_id)
        return

    logger.debug("  提取文本长度: %d 字符", len(doc_text))

    # 5. 处理元数据
    from text_processing import (
        CHUNK_MAX_WORDS,
        chunk_text,
        extract_year_from_date,
    )

    doi_norm = item["doi"].strip().lower().rstrip(".") if item["doi"] else ""
    authors = format_authors(cursor, item_id)
    collections = lookup_collections(cursor, item_id, collection_map)
    year = extract_year_from_date(item["date"])

    # 6. 分块
    chunks = chunk_text(doc_text, title=item["title"], max_words=CHUNK_MAX_WORDS)
    if not chunks:
        logger.info("  -> 跳过: 分块结果为空")
        stats["skipped_extract_fail"] += 1
        pending_item_ids.add(item_id)
        return

    logger.debug("  分块: %d 个", len(chunks))

    # 7. 构建 ChromaDB 记录
    ids = [f"{paper_id}#{c['chunk_index']}" for c in chunks]
    documents = [c["text"] for c in chunks]

    # 将摘要注入第一个 chunk 之前（增强搜索能力）
    if item["abstract_note"] and documents:
        doc0_parts = [item["abstract_note"], documents[0]]
        documents[0] = "\n\n".join(doc0_parts)

    metadatas = [
        {
            "paper_id": paper_id,
            "title": item["title"],
            "filename": att_path.name,
            "section": c.get("section", ""),
            "chunk_index": c["chunk_index"],
            "total_chunks": len(chunks),
            # Zotero 增强字段
            "source": "zotero",
            "doi": doi_norm,
            "zotero_item_id": item_id,
            "zotero_key": item["key"],
            "authors": authors,
            "journal": item["journal"],
            "year": year,
            "collections": "; ".join(collections),
        }
        for c in chunks
    ]

    # 8. 生成嵌入
    try:
        if model is None:
            raise RuntimeError("嵌入模型未加载")
        embeddings = model.encode(
            documents,
            batch_size=32,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
    except Exception as e:
        logger.warning("  嵌入失败: %s", e)
        stats["errors"].append((item["title"], str(e)))
        pending_item_ids.add(item_id)
        return

    # 9. 写入 ChromaDB
    try:
        replace_paper_chunks(
            collection,
            paper_id=paper_id,
            ids=ids,
            embeddings=embeddings,
            documents=documents,
            metadatas=metadatas,
        )
    except Exception as e:
        logger.warning("  写入 ChromaDB 失败: %s", e)
        stats["errors"].append((item["title"], str(e)))
        pending_item_ids.add(item_id)
        return

    stats["imported"] += 1
    logger.info("  ✓ 导入成功 (%d chunks)", len(chunks))


def _sync_items(
    items: list[dict],
    *,
    cursor,
    collection,
    model,
    collection_map: dict[int, dict],
    zotero_storage: Path,
    dry_run: bool,
    mineru_timeout_hours: float,
) -> tuple[dict, set[int]]:
    """遍历处理所有待同步论文项。"""
    stats = _init_stats(len(items))
    pending_item_ids: set[int] = set()
    mineru_output_dir = REPO_ROOT / "kb" / "mineru_cache"
    mineru_output_dir.mkdir(parents=True, exist_ok=True)

    for item in items:
        _process_single_item(
            item,
            cursor=cursor,
            collection=collection,
            model=model,
            collection_map=collection_map,
            zotero_storage=zotero_storage,
            mineru_output_dir=mineru_output_dir,
            dry_run=dry_run,
            mineru_timeout_hours=mineru_timeout_hours,
            stats=stats,
            pending_item_ids=pending_item_ids,
        )

    return stats, pending_item_ids


def _sync_deletions(
    cursor, collection, last_version: int, dry_run: bool
) -> int:
    """清理 ChromaDB 中已在 Zotero 中被删除的论文。"""
    if dry_run:
        return 0
    logger.info("检查 Zotero 中已删除的论文...")
    try:
        removed = cleanup_deleted_items(cursor, collection, last_version)
        if removed:
            logger.info("已清理 %d 篇已删除论文", removed)
        return removed
    except Exception as e:
        logger.warning("删除同步失败: %s", e)
        return 0


def _save_checkpoint_state(
    checkpoint: dict,
    current_version: int,
    items: list[dict],
    pending_item_ids: set[int],
    dry_run: bool,
) -> None:
    """保存检查点（dry-run 模式跳过）。"""
    if dry_run:
        return
    max_item_id = compute_next_last_item_id(checkpoint["last_item_id"], items)
    save_checkpoint({
        "last_item_id": max_item_id,
        "last_version": current_version,
        "pending_item_ids": sorted(pending_item_ids),
    })
    logger.info("检查点已保存 (item_id=%d, version=%d)", max_item_id, current_version)
    if pending_item_ids:
        logger.warning("  %d 个项目将在下次同步时重试", len(pending_item_ids))


def _rebuild_indexes(
    *, dry_run: bool, skip_build_index: bool, imported: int, removed: int
) -> None:
    """重建 FTS 文本索引 + 更新集合描述信息。"""
    if dry_run:
        return

    if not skip_build_index and (imported > 0 or removed > 0):
        logger.info("\n检测到知识库变更，重建文本搜索索引...")
        try:
            from build_index import main as build_index_main
            build_index_main()
        except Exception as e:
            logger.warning("文本索引重建失败: %s", e)
            logger.info("可稍后手动运行: python scripts/build_index.py")

    try:
        from generate_collection_info import main as gen_info_main
        gen_info_main()
    except Exception as e:
        logger.warning("集合信息生成失败: %s", e)


def _print_summary(stats: dict, dry_run: bool, elapsed: float) -> None:
    """打印同步汇总。"""
    logger.info("\n" + "=" * 55)
    logger.info("同步完成!")
    logger.info("  总计:            %d 篇", stats["total"])
    logger.info("  已导入:           %d 篇", stats["imported"])
    logger.info("  跳过（无附件）:   %d 篇", stats["skipped_no_attachment"])
    logger.info("  跳过（已有）:     %d 篇", stats["skipped_dup"])
    logger.info("  跳过（提取失败）: %d 篇", stats["skipped_extract_fail"])

    if stats["errors"]:
        logger.info("\n错误 (%d 个):", len(stats["errors"]))
        for title, err in stats["errors"][:5]:
            logger.info("  - %s: %s", title[:50], err)

    if dry_run:
        logger.info("\n[dry-run] 模式完成，未实际写入任何数据")

    logger.info("=" * 55)
    logger.info("  耗时: %.1f 秒", elapsed)
    logger.info("=" * 55)


def _main_unlocked():
    """主流程：解析参数 → 连接 Zotero → 同步论文 → 删除同步 → 重建索引 → 汇总。"""
    # ── 快速路径：--version 不触发任何延迟导入 ──────────────
    if "--version" in sys.argv[1:]:
        print(f"paper-knowledge-base {get_version(REPO_ROOT.parent)}")
        sys.exit(0)

    args = _parse_args()

    zotero_dir: Path = args.zotero_dir
    zotero_storage = zotero_dir / "storage"
    dry_run = args.dry_run
    _start_time = time.time()

    logger.info("=" * 55)
    logger.info("Zotero 同步开始")
    logger.info("  Zotero 目录: %s", zotero_dir)
    logger.info("  知识库根目录: %s", REPO_ROOT)
    _resolve_mineru_paths(args)
    logger.info("  Dry-run: %s", dry_run)
    logger.info("=" * 55)

    if not zotero_storage.is_dir():
        logger.error("Zotero storage 目录不存在: %s", zotero_storage)
        sys.exit(1)

    # ── 连接 Zotero DB ──────────────────────────────────────
    logger.info("连接 Zotero 数据库...")
    zotero_conn = get_zotero_db_connection(zotero_dir)
    cursor = zotero_conn.cursor()

    # ── 加载检查点 ─────────────────────────────────────────
    if args.full_rescan:
        checkpoint = {"last_item_id": 0, "last_version": 0, "pending_item_ids": []}
        logger.info("强制全量重建")
    else:
        checkpoint = load_checkpoint()
        logger.info("上次同步: item_id=%d, version=%d",
                     checkpoint["last_item_id"], checkpoint["last_version"])

    current_version = check_zotero_version(cursor)
    logger.info("Zotero 当前版本: %d", current_version)

    # ── 获取论文列表 ───────────────────────────────────────
    items = get_paper_items(
        cursor,
        since_item_id=checkpoint["last_item_id"],
        since_version=checkpoint["last_version"],
        pending_item_ids=checkpoint.get("pending_item_ids", []),
    )
    logger.info("待处理的论文数: %d", len(items))

    # ── 连接 ChromaDB ──────────────────────────────────────
    logger.info("连接向量数据库...")
    collection = get_or_create_chroma_collection()

    # Dry-run 不会生成嵌入；删除-only 同步也不需要加载大模型。
    model = None
    if items and not dry_run:
        logger.info("加载嵌入模型...")
        model = load_bi_encoder()
    elif not items:
        logger.info("没有新增或修改的论文，继续检查删除同步")

    # ── 加载 collections 表 + 同步论文 ─────────────────────
    collection_map = _load_collection_map(cursor)
    stats, pending_item_ids = _sync_items(
        items,
        cursor=cursor,
        collection=collection,
        model=model,
        collection_map=collection_map,
        zotero_storage=zotero_storage,
        dry_run=dry_run,
        mineru_timeout_hours=args.mineru_timeout_hours,
    )

    # ── 删除同步 ───────────────────────────────────────────
    removed = _sync_deletions(cursor, collection, checkpoint["last_version"], dry_run)

    # ── 保存检查点 ─────────────────────────────────────────
    _save_checkpoint_state(
        checkpoint, current_version, items, pending_item_ids, dry_run
    )

    # ── 重建 FTS 索引 + 集合信息 ──────────────────────────
    _rebuild_indexes(
        dry_run=dry_run,
        skip_build_index=args.skip_build_index,
        imported=stats["imported"],
        removed=removed,
    )

    # ── 汇总 ──────────────────────────────────────────────
    elapsed = time.time() - _start_time
    _print_summary(stats, dry_run, elapsed)

    # 关闭 Zotero 连接
    zotero_conn.close()


def main():
    if "--version" in sys.argv[1:]:
        return _main_unlocked()
    with SyncProcessLock(SYNC_LOCK_FILE):
        return _main_unlocked()


if __name__ == "__main__":
    main()
