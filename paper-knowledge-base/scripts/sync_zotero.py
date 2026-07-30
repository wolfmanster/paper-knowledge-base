"""
Zotero → 论文知识库 同步脚本
============================
从 Zotero 的本地 SQLite 数据库中读取论文元数据和 PDF 附件，
通过 MinerU 提取文本，分块嵌入后写入 ChromaDB。

本模块是同步主入口与门面（facade）：常量、日志、检查点、主流程留在此处，
其余职责拆分到同级子模块：
  - sync_process_lock.py   跨平台进程锁
  - zotero_reader.py       Zotero SQLite 查询
  - mineru_runner.py       MinerU subprocess 提取与进程管理
  - chroma_writer.py       ChromaDB 写入、去重、删除同步

用法:
  python scripts/sync_zotero.py                   # 全量/增量同步
  python scripts/sync_zotero.py --dry-run         # 试运行
  python scripts/sync_zotero.py --full-rescan     # 强制全量重建
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import time
from pathlib import Path

# ── 路径与常量 ───────────────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parent.parent

SCRIPTS_DIR = REPO_ROOT / "scripts"
CHROMA_DIR = REPO_ROOT / "kb" / "chroma"
CHECKPOINT_FILE = REPO_ROOT / "kb" / "zotero_checkpoint.json"
LOG_FILE = REPO_ROOT / "kb" / "sync_zotero.log"
SYNC_LOCK_FILE = REPO_ROOT / "kb" / "sync_zotero.lock"
DEFAULT_ZOTERO_DIR = Path.home() / "Zotero"
_DEFAULT_MINERU_DIR = (SCRIPTS_DIR.parent.parent / "MinerU-GUI").resolve()

# ── 子模块符号 re-export ─────────────────────────────────────
# 必须在 _main_unlocked 之前 import，使 sync_zotero.<name> 可被测试
# 通过 monkeypatch.setattr(sync_zotero, "<name>", ...) 替换；
# _main_unlocked 内部以裸名字调用这些符号，会查 sync_zotero 模块 globals，
# 因此 monkeypatch 能生效。
from sync_process_lock import SyncProcessLock  # noqa: E402
from zotero_reader import (  # noqa: E402
    get_zotero_db_connection,
    get_paper_items,
    get_attachment_info,
    lookup_collections,
    format_authors,
    check_zotero_version,
)
from mineru_runner import (  # noqa: E402
    resolve_mineru_python,
    resolve_attachment_path,
    run_captured_process,
    extract_with_mineru,
    MINERU_DIR,
    MINERU_PYTHON,
    MINERU_SCRIPT,
    DEFAULT_MINERU_TIMEOUT_SECONDS,
)
from chroma_writer import (  # noqa: E402
    load_bi_encoder,
    get_or_create_collection,
    deduplicate_paper,
    _normalize_title,
    _get_existing_zotero_item_id,
    is_duplicate_zotero_item,
    replace_paper_chunks,
    cleanup_deleted_items,
    COLLECTION_NAME,
)

# ── 日志（模块加载时配置，沿用当前 stdout）────────────────────
CHROMA_DIR.mkdir(parents=True, exist_ok=True)

# 日志沿用当前 stdout；不要重新包装进程文件描述符，否则导入模块的宿主
# （pytest、IDE、notebook 等）可能在包装器销毁时失去自己的输出流。
_log_stream = sys.stdout
if hasattr(_log_stream, "reconfigure"):
    try:
        _log_stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(_log_stream),
        logging.FileHandler(str(LOG_FILE), mode="a", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
#  检查点管理
#  留在主模块：CHECKPOINT_FILE 被 monkeypatch.setattr(sync_zotero, ...)
#  替换时，save/load_checkpoint 必须读取 sync_zotero.CHECKPOINT_FILE。
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

def _main_unlocked():
    import argparse
    import mineru_runner

    # ── 参数解析 ──────────────────────────────────────────
    parser = argparse.ArgumentParser(
        description="从 Zotero 同步论文到知识库",
        add_help=False,
    )
    parser.add_argument("--version", action="store_true",
                        help="显示版本号并退出")

    # 快速路径：--version 不触发任何延迟导入
    known, _ = parser.parse_known_args()
    if known.version:
        _version_file = REPO_ROOT.parent / "_version.py"
        if _version_file.exists():
            _ver: dict[str, str] = {}
            exec(_version_file.read_text(encoding="utf-8"), _ver)
            print(f"paper-knowledge-base {_ver.get('__version__', 'unknown')}")
        else:
            print("paper-knowledge-base unknown (no _version.py)")
        sys.exit(0)

    # 延迟导入（知识库环境中的依赖）
    import chromadb
    # 知识库模块
    sys.path.insert(0, str(SCRIPTS_DIR))
    from utils import (
        CHUNK_MAX_WORDS,
        chunk_text,
        compute_paper_id_from_doi,
        extract_year_from_date,
    )

    # ── 参数解析 ──────────────────────────────────────────
    parser = argparse.ArgumentParser(description="从 Zotero 同步论文到知识库")
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

    # 解析 MinerU 路径：CLI > 环境变量 > ../MinerU-GUI（monorepo 回退）
    if args.mineru_dir is not None:
        _md = args.mineru_dir.resolve()
    else:
        _env = os.environ.get("MINERU_DIR", "").strip()
        _md = Path(_env).resolve() if _env else (
            _DEFAULT_MINERU_DIR if _DEFAULT_MINERU_DIR.is_dir() else None
        )

    # MinerU 全局存放在 mineru_runner 模块；extract_with_mineru 读取它们。
    mineru_runner.MINERU_DIR = _md
    mineru_runner.MINERU_PYTHON = resolve_mineru_python(_md) if _md else None
    # MINERU_SCRIPT 不受此影响，始终在项目 scripts/ 下

    zotero_dir: Path = args.zotero_dir
    zotero_storage = zotero_dir / "storage"
    dry_run = args.dry_run
    _start_time = time.time()

    logger.info("=" * 55)
    logger.info("Zotero 同步开始")
    logger.info("  Zotero 目录: %s", zotero_dir)
    logger.info("  知识库根目录: %s", REPO_ROOT)
    if mineru_runner.MINERU_PYTHON:
        logger.info("  MinerU: %s", mineru_runner.MINERU_PYTHON)
    else:
        logger.info("  MinerU: 未配置（PDF 提取将跳过）")
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
    client = chromadb.PersistentClient(str(CHROMA_DIR))
    collection = get_or_create_collection(client)

    # Dry-run 不会生成嵌入；删除-only 同步也不需要加载大模型。
    model = None
    if items and not dry_run:
        logger.info("加载嵌入模型...")
        model = load_bi_encoder()
    elif not items:
        logger.info("没有新增或修改的论文，继续检查删除同步")

    # ── 加载 collections 表到内存（N+1 优化） ──────────────
    logger.info("加载 Zotero 分类结构...")
    collection_map: dict[int, dict] = {}
    for row in cursor.execute("SELECT collectionID, collectionName, parentCollectionID FROM collections"):
        collection_map[row["collectionID"]] = {
            "name": row["collectionName"],
            "parent": row["parentCollectionID"],
        }
    logger.debug("  加载了 %d 个集合", len(collection_map))

    # ── 统计 ───────────────────────────────────────────────
    stats = {
        "total": len(items),
        "skipped_no_attachment": 0,
        "skipped_dup": 0,
        "skipped_extract_fail": 0,
        "imported": 0,
        "errors": [],
    }
    pending_item_ids: set[int] = set()

    mineru_output_dir = REPO_ROOT / "kb" / "mineru_cache"
    mineru_output_dir.mkdir(parents=True, exist_ok=True)

    # ── 逐篇处理 ───────────────────────────────────────────
    for idx, item in enumerate(items, 1):
        item_id = item["item_id"]
        title_display = item["title"][:60] if item["title"] else "(无标题)"

        logger.info("[%d/%d] 处理: %s", idx, stats["total"], title_display)
        logger.debug("  itemID=%d, key=%s, DOI=%s", item_id, item["key"],
                     item["doi"][:60] if item["doi"] else "N/A")

        # 1. 计算 paper_id
        if item["doi"]:
            paper_id = compute_paper_id_from_doi(item["doi"])
        else:
            # 无 DOI 时用 Zotero key + itemID 生成确定性的 ID
            raw = f"zotero:{item['key']}:{item_id}".encode("utf-8")
            paper_id = hashlib.sha256(raw).hexdigest()[:12]
            logger.debug("  无 DOI，使用 zotero key 计算 paper_id: %s", paper_id)

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
                continue
            # 已同步 Zotero 项的版本变化应覆盖原记录；全量重扫也沿用已有 ID。
            paper_id = existing_id
            logger.debug("  更新已有论文: paper_id=%s", paper_id)

        # 3. 获取附件
        attachment = get_attachment_info(cursor, item_id)
        if not attachment:
            logger.info("  -> 跳过: 无 PDF 或 Word 附件")
            stats["skipped_no_attachment"] += 1
            pending_item_ids.add(item_id)
            continue

        att_key = attachment["key"]
        content_type = attachment["content_type"]
        att_path = resolve_attachment_path(zotero_storage, att_key, content_type)
        if not att_path:
            logger.info("  -> 跳过: storage 中未找到附件 (key=%s, type=%s)", att_key, content_type)
            stats["skipped_no_attachment"] += 1
            pending_item_ids.add(item_id)
            continue

        type_label = "Word" if content_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document" else "PDF"
        logger.info("  %s: %s", type_label, att_path.name)

        if dry_run:
            logger.info("  [dry-run] 将提取并导入此论文")
            continue

        # 4. MinerU 提取文本
        mineru_result = extract_with_mineru(
            att_path,
            mineru_output_dir,
            timeout_seconds=args.mineru_timeout_hours * 3600,
        )
        if not mineru_result:
            logger.info("  -> 跳过: MinerU 提取失败")
            stats["skipped_extract_fail"] += 1
            pending_item_ids.add(item_id)
            continue

        doc_text = mineru_result.get("text", "")
        if not doc_text or len(doc_text.strip()) < 20:
            logger.info("  -> 跳过: 提取文本过短")
            stats["skipped_extract_fail"] += 1
            pending_item_ids.add(item_id)
            continue

        logger.debug("  提取文本长度: %d 字符", len(doc_text))

        # 5. 处理元数据
        doi_norm = item["doi"].strip().lower().rstrip(".") if item["doi"] else ""
        authors = format_authors(cursor, item_id)
        collections = lookup_collections(cursor, item_id, collection_map)
        year = extract_year_from_date(item["date"])

        # 6. 构建 enriched 全文（将 Zotero 的 abstractNote 前置）
        full_text = doc_text
        if item["abstract_note"]:
            # 如果 MinerU 提取的文本中也包含摘要，不必再前置
            # 但仍确保 metadata 中有摘要
            pass

        # 7. 分块
        chunks = chunk_text(full_text, title=item["title"], max_words=CHUNK_MAX_WORDS)
        if not chunks:
            logger.info("  -> 跳过: 分块结果为空")
            stats["skipped_extract_fail"] += 1
            pending_item_ids.add(item_id)
            continue

        logger.debug("  分块: %d 个", len(chunks))

        # 8. 构建 ChromaDB 记录
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

        # 9. 生成嵌入
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
            continue

        # 10. 写入 ChromaDB
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
            continue

        stats["imported"] += 1
        logger.info("  ✓ 导入成功 (%d chunks)", len(chunks))

    # ── 删除同步 ───────────────────────────────────────────
    removed = 0
    if not dry_run:
        logger.info("检查 Zotero 中已删除的论文...")
        try:
            removed = cleanup_deleted_items(cursor, collection, checkpoint["last_version"])
            if removed:
                logger.info("已清理 %d 篇已删除论文", removed)
        except Exception as e:
            logger.warning("删除同步失败: %s", e)

    # ── 保存检查点 ─────────────────────────────────────────
    if not dry_run:
        max_item_id = compute_next_last_item_id(checkpoint["last_item_id"], items)
        save_checkpoint({
            "last_item_id": max_item_id,
            "last_version": current_version,
            "pending_item_ids": sorted(pending_item_ids),
        })
        logger.info("检查点已保存 (item_id=%d, version=%d)", max_item_id, current_version)
        if pending_item_ids:
            logger.warning("  %d 个项目将在下次同步时重试", len(pending_item_ids))

    # ── 重建 FTS 索引 ─────────────────────────────────────
    if not dry_run and not args.skip_build_index and (stats["imported"] > 0 or removed > 0):
        logger.info("\n检测到知识库变更，重建文本搜索索引...")
        try:
            from build_index import main as build_index_main
            build_index_main()
        except Exception as e:
            logger.warning("文本索引重建失败: %s", e)
            logger.info("可稍后手动运行: python scripts/build_index.py")

    # ── 更新集合描述信息 ──────────────────────────────────
    if not dry_run:
        try:
            from generate_collection_info import main as gen_info_main
            gen_info_main()
        except Exception as e:
            logger.warning("集合信息生成失败: %s", e)

    # ── 汇总 ──────────────────────────────────────────────
    elapsed = time.time() - _start_time

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

    # 关闭 Zotero 连接
    zotero_conn.close()


def main():
    if "--version" in sys.argv[1:]:
        return _main_unlocked()
    with SyncProcessLock(SYNC_LOCK_FILE):
        return _main_unlocked()


if __name__ == "__main__":
    main()
