"""
Zotero → 论文知识库 同步脚本
================================
从 Zotero 的本地 SQLite 数据库中读取论文元数据和 PDF 附件，
通过 MinerU 提取文本，分块嵌入后写入 ChromaDB。

用法:
  python scripts/sync_zotero.py                   # 全量/增量同步
  python scripts/sync_zotero.py --dry-run         # 试运行
  python scripts/sync_zotero.py --full-rescan     # 强制全量重建
"""

from __future__ import annotations

import atexit
import hashlib
import json
import logging
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Optional

# ── 路径 ─────────────────────────────────────────────────────
# 尝试定位知识库根目录（兼容工作树环境）
CANDIDATE_ROOTS = [
    Path(__file__).resolve().parent.parent,                          # 普通目录
    Path(__file__).resolve().parent.parent.parent.parent.parent,     # 工作树
]

REPO_ROOT: Path | None = None
for _c in CANDIDATE_ROOTS:
    if (_c / "Papers").exists() and (_c / "kb").exists():
        REPO_ROOT = _c.resolve()
        break
if REPO_ROOT is None:
    REPO_ROOT = Path(__file__).resolve().parent.parent

SCRIPTS_DIR = REPO_ROOT / "scripts"
CHROMA_DIR = REPO_ROOT / "kb" / "chroma"
CHECKPOINT_FILE = REPO_ROOT / "kb" / "zotero_checkpoint.json"
LOG_FILE = REPO_ROOT / "kb" / "sync_zotero.log"
COLLECTION_NAME = "papers"

# Zotero 默认路径
DEFAULT_ZOTERO_DIR = Path.home() / "Zotero"
# MinerU 路径：通过环境变量 MINERU_DIR 或 CLI 参数 --mineru-dir 指定
# 模块加载时为 None，在 main() 执行时解析路径
# 默认回退：尝试 ../MinerU-GUI（monorepo 兄弟目录）
MINERU_DIR: Path | None = None
MINERU_PYTHON: Path | None = None
MINERU_SCRIPT = SCRIPTS_DIR / "mineru_extract.py"
_DEFAULT_MINERU_DIR = (SCRIPTS_DIR.parent.parent / "MinerU-GUI").resolve()

# 在模块层级维护 Zotero 连接引用，供 atexit 清理
_ZOTERO_CONN: sqlite3.Connection | None = None

# ── 日志 ─────────────────────────────────────────────────────
CHROMA_DIR.mkdir(parents=True, exist_ok=True)

# 日志：stdout 用 UTF-8 编码（避免 ✓φ∑ 等 Unicode 在 GBK 终端报错）
_log_stream = open(sys.__stdout__.fileno(), mode="w", encoding="utf-8", buffering=1, closefd=False)
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
#  Zotero 数据库连接与查询
# ═══════════════════════════════════════════════════════════════

def get_zotero_db_connection(zotero_dir: Path) -> sqlite3.Connection:
    """连接到 Zotero 的 zotero.sqlite 数据库（只读）。"""
    db_path = zotero_dir / "zotero.sqlite"
    if not db_path.exists():
        logger.error("Zotero 数据库不存在: %s", db_path)
        logger.error("请确认 Zotero 数据目录，用 --zotero-dir 指定")
        sys.exit(1)

    try:
        # 使用 URI 模式以只读方式打开，防止 WAL checkpoint 污染 Zotero 数据库
        db_uri = f"file:{db_path.resolve()}?mode=ro"
        conn = sqlite3.connect(db_uri, uri=True, timeout=5)
        # 处理 UTF-8 中文（Zotero 7 的 itemDataValues.value 是 blob）
        conn.text_factory = lambda x: str(x, "utf-8", errors="replace")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.row_factory = sqlite3.Row
        # 注册 atexit 清理，确保意外退出也关闭连接（幂等：close 可多次调用）
        global _ZOTERO_CONN
        _ZOTERO_CONN = conn
        atexit.register(lambda c=conn: c.close() if c else None)
        return conn
    except sqlite3.DatabaseError as e:
        logger.error("Zotero 数据库无法打开: %s", e)
        logger.error("请关闭 Zotero 后重试")
        sys.exit(1)


def get_paper_items(
    cursor,
    since_item_id: int = 0,
    since_version: int = 0,
) -> list[dict]:
    """获取 Zotero 中所有可检索的论文项及其元数据。

    可检索类型: journalArticle, conferencePaper, thesis, preprint, computerProgram
    排除: 已被删除的项

    Args:
        since_item_id: 仅返回 itemID > 此值的项（增量同步）
        since_version: 仅返回 version > 此值的项（增量同步）

    Returns:
        论文项字典列表，每项包含 title, abstractNote, doi, 等
    """
    rows = cursor.execute(
        """
        SELECT i.itemID, i.key, i.dateAdded, i.dateModified, i.version,
               t.typeName,
               (SELECT v.value FROM itemData d JOIN itemDataValues v
                  ON d.valueID = v.valueID
                WHERE d.itemID = i.itemID AND d.fieldID = 1
                LIMIT 1) AS title,
               (SELECT v.value FROM itemData d JOIN itemDataValues v
                  ON d.valueID = v.valueID
                WHERE d.itemID = i.itemID AND d.fieldID = 2
                LIMIT 1) AS abstractNote,
               (SELECT v.value FROM itemData d JOIN itemDataValues v
                  ON d.valueID = v.valueID
                WHERE d.itemID = i.itemID AND d.fieldID = 59
                LIMIT 1) AS doi,
               (SELECT v.value FROM itemData d JOIN itemDataValues v
                  ON d.valueID = v.valueID
                WHERE d.itemID = i.itemID AND d.fieldID = 38
                LIMIT 1) AS publicationTitle,
               (SELECT v.value FROM itemData d JOIN itemDataValues v
                  ON d.valueID = v.valueID
                WHERE d.itemID = i.itemID AND d.fieldID = 6
                LIMIT 1) AS date,
               (SELECT v.value FROM itemData d JOIN itemDataValues v
                  ON d.valueID = v.valueID
                WHERE d.itemID = i.itemID AND d.fieldID = 13
                LIMIT 1) AS url
        FROM items i
        JOIN itemTypes t ON i.itemTypeID = t.itemTypeID
        WHERE t.typeName IN ('journalArticle','conferencePaper','thesis','preprint','computerProgram')
          AND i.itemID NOT IN (SELECT itemID FROM deletedItems)
          AND (i.itemID > ? OR i.version > ?)
        ORDER BY i.itemID
        """,
        (since_item_id, since_version),
    ).fetchall()

    items = []
    for r in rows:
        item = {
            "item_id": r["itemID"],
            "key": r["key"],
            "type": r["typeName"],
            "title": r["title"] or "",
            "abstract_note": r["abstractNote"] or "",
            "doi": r["doi"] or "",
            "journal": r["publicationTitle"] or "",
            "date": r["date"] or "",
            "version": r["version"],
        }
        items.append(item)

    return items


def get_attachment_info(cursor, parent_item_id: int) -> Optional[str]:
    """获取论文的 PDF 附件信息。

    Args:
        parent_item_id: 父论文项 ID

    Returns:
        attachment_key (items.key 的值，即 storage/ 下的目录名)，
        如果没有 PDF 附件则返回 None
    """
    row = cursor.execute(
        """
        SELECT i.key
        FROM itemAttachments ia
        JOIN items i ON ia.itemID = i.itemID
        WHERE ia.parentItemID = ?
          AND ia.contentType = 'application/pdf'
          AND ia.linkMode = 0
        LIMIT 1
        """,
        (parent_item_id,),
    ).fetchone()

    return row["key"] if row else None


def lookup_collections(cursor, item_id: int,
                       collection_map: dict[int, dict] | None = None) -> list[str]:
    """查找论文所属的 Zotero 集合路径列表。

    使用内存中的 collection_map 避免 N+1 SQL 查询。如果未提供 map，回退到逐条查询。

    例如 item 同时在 "LC" 和 "CTP" 下，返回 ["LC", "CTP"]。
    对于嵌套集合如 LC/仿生，返回完整路径 ["LC", "LC/仿生"]。

    Args:
        cursor: 数据库游标
        item_id: 论文项 ID
        collection_map: 预加载的 {collectionID: {"name": str, "parent": int|None}} 映射

    Returns:
        集合路径列表（扁平字符串）
    """
    rows = cursor.execute(
        """
        SELECT cl.collectionID
        FROM collectionItems ci
        JOIN collections cl ON ci.collectionID = cl.collectionID
        WHERE ci.itemID = ?
        ORDER BY cl.collectionName
        """,
        (item_id,),
    ).fetchall()

    def _resolve_path(cid: int) -> list[str]:
        """使用内存 map 递归构建集合路径。"""
        if collection_map and cid in collection_map:
            parts = [collection_map[cid]["name"]]
            pid = collection_map[cid]["parent"]
            while pid:
                if pid in collection_map:
                    parts.insert(0, collection_map[pid]["name"])
                    pid = collection_map[pid]["parent"]
                else:
                    break
            return parts
        # 回退到 SQL 查询
        parts = []
        current_id = cid
        while current_id:
            row = cursor.execute(
                "SELECT collectionName, parentCollectionID FROM collections WHERE collectionID = ?",
                (current_id,),
            ).fetchone()
            if row:
                parts.insert(0, row["collectionName"])
                current_id = row["parentCollectionID"]
            else:
                break
        return parts

    paths = []
    for r in rows:
        full = "/".join(_resolve_path(r["collectionID"]))
        if full:
            paths.append(full)

    return paths


def format_authors(cursor, item_id: int, max_authors: int = 3) -> str:
    """格式化论文的作者列表。

    Args:
        item_id: 论文项 ID
        max_authors: 最多列出几位作者

    Returns:
        格式如 "Jin, Leilei; Xi, Huan" 或 "Jin, Leilei et al."
    """
    rows = cursor.execute(
        """
        SELECT c.lastName, c.firstName
        FROM itemCreators ic
        JOIN creators c ON ic.creatorID = c.creatorID
        WHERE ic.itemID = ? AND ic.creatorTypeID = 1  -- 1 = author
        ORDER BY ic.orderIndex
        """,
        (item_id,),
    ).fetchall()

    if not rows:
        return ""

    authors = []
    for r in rows:
        last = (r["lastName"] or "").strip()
        first = (r["firstName"] or "").strip()
        if last and first:
            authors.append(f"{last}, {first}")
        elif last:
            authors.append(last)
        else:
            authors.append(first)

    if len(authors) <= max_authors:
        return "; ".join(authors)
    else:
        return "; ".join(authors[:max_authors]) + " et al."


def check_zotero_version(cursor) -> int:
    """获取 Zotero 当前版本号（用于增量同步检测）。"""
    row = cursor.execute("SELECT MAX(version) FROM items").fetchone()
    return row[0] or 0


# ═══════════════════════════════════════════════════════════════
#  检查点管理
# ═══════════════════════════════════════════════════════════════

def load_checkpoint() -> dict:
    """从检查点文件加载上次同步状态。

    Returns:
        {"last_item_id": int, "last_version": int}
    """
    default = {"last_item_id": 0, "last_version": 0}
    if not CHECKPOINT_FILE.exists():
        return default

    try:
        data = json.loads(CHECKPOINT_FILE.read_text(encoding="utf-8"))
        return {
            "last_item_id": data.get("last_item_id", 0),
            "last_version": data.get("last_version", 0),
        }
    except (json.JSONDecodeError, KeyError) as e:
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


# ═══════════════════════════════════════════════════════════════
#  PDF 提取（MinerU subprocess）
# ═══════════════════════════════════════════════════════════════

def resolve_pdf_path(zotero_storage: Path, attachment_key: str, max_size_mb: int = 25) -> Optional[Path]:
    """在 Zotero storage/ 目录中查找 PDF 文件。

    跳过超过 max_size_mb 的超大文件（通常是手册/书籍而非研究论文）。

    Args:
        zotero_storage: 指向 Zotero 的 storage/ 目录
        attachment_key: 附件项的 items.key
        max_size_mb: 文件大小上限（MB），超过返回 None

    Returns:
        第一个找到的 .pdf 文件路径，未找到返回 None
    """
    storage_dir = zotero_storage / attachment_key
    if not storage_dir.is_dir():
        logger.debug("  storage 目录不存在: %s", storage_dir)
        return None

    pdfs = sorted(storage_dir.glob("*.pdf"))
    if not pdfs:
        logger.debug("  目录中无 PDF 文件: %s", storage_dir)
        return None

    pdf_path = pdfs[0]
    if len(pdfs) > 1:
        logger.debug("  storage 目录中有多个 PDF，取第一个: %s", pdf_path.name)

    # 超大文件检查
    max_bytes = max_size_mb * 1024 * 1024
    try:
        if pdf_path.stat().st_size > max_bytes:
            logger.warning("  PDF 文件过大 (>%dMB)，跳过: %s (%.1fMB)",
                           max_size_mb, pdf_path.name, pdf_path.stat().st_size / (1024*1024))
            return None
    except OSError as e:
        logger.warning("  无法读取 PDF 文件大小: %s — %s", pdf_path.name, e)
        return None

    return pdf_path


def extract_pdf_with_mineru(pdf_path: Path, output_dir: Path) -> Optional[dict]:
    """通过 MinerU subprocess 提取 PDF 文本。

    调用 mineru_extract.py（在 MinerU venv 中运行），
    读取 stdout 中的 JSON 结果。

    Args:
        pdf_path: PDF 文件路径
        output_dir: MinerU 输出目录

    Returns:
        {"text": "pure text", "markdown": "raw md"}，失败返回 None
    """
    if MINERU_PYTHON is None:
        logger.error("  MinerU 路径未配置，跳过 PDF 提取")
        logger.error("  请通过 --mineru-dir 参数或 MINERU_DIR 环境变量指定 MinerU GUI 目录")
        return None

    if not MINERU_PYTHON.exists():
        logger.error("  MinerU Python 不存在: %s", MINERU_PYTHON)
        logger.error("  请确认 MinerU GUI 路径")
        return None

    if not MINERU_SCRIPT.exists():
        logger.error("  mineru_extract.py 不存在: %s", MINERU_SCRIPT)
        return None

    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        # 设置 MINERU_GUI_DIR 环境变量，让 mineru_extract.py 能找到 mineru_api
        env = os.environ.copy()
        env["MINERU_GUI_DIR"] = str(MINERU_DIR)

        proc = subprocess.run(
            [
                str(MINERU_PYTHON),
                str(MINERU_SCRIPT),
                str(pdf_path),
                str(output_dir),
                "--lang", "en",
                "--max_pages", "20",
            ],
            capture_output=True,
            text=True,
            timeout=600,  # 10 分钟超时（pipeline CPU 模式较慢）
            encoding="utf-8",
            errors="replace",
            env=env,
        )

        # 解析输出 JSON
        if proc.returncode != 0:
            logger.warning("  MinerU 进程返回错误码 %d", proc.returncode)
            logger.debug("  stderr: %s", proc.stderr[:500])
            return None

        result = json.loads(proc.stdout.strip())
        if result.get("status") == "ok":
            return result
        else:
            logger.warning("  MinerU 提取失败: %s", result.get("message", "未知错误"))
            return None

    except subprocess.TimeoutExpired:
        logger.warning("  MinerU 超时 (5min): %s", pdf_path.name)
        return None
    except json.JSONDecodeError as e:
        logger.warning("  MinerU 输出解析失败: %s", e)
        logger.debug("  原始输出: %s", proc.stdout[:300] if proc else "N/A")
        return None
    except Exception as e:
        logger.warning("  MinerU 调用失败: %s", e)
        return None


# ═══════════════════════════════════════════════════════════════
#  导入模型与 ChromaDB
# ═══════════════════════════════════════════════════════════════

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


def deduplicate_paper(doi: str, title: str, collection) -> Optional[str]:
    """检查论文是否已在 ChromaDB 中。

    三层去重策略:
    1. DOI 精确匹配（优先）
    2. 标题 $contains 匹配（回退）

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

    # 第二层：标题相似度匹配
    if title:
        # 提取标题中的关键词（取前 2 个非停用词）
        import re
        title_norm = re.sub(r"[^\w\s]", "", title).lower().strip()
        words = [w for w in title_norm.split() if len(w) > 3]
        if len(words) >= 2:
            query_terms = " ".join(words[:3])
            try:
                result = collection.get(
                    include=["metadatas"],
                    where={"title": {"$contains": query_terms}},
                    limit=3,
                )
                if result["ids"]:
                    logger.debug("  标题匹配成功: '%s' -> %s", query_terms, result["ids"][0])
                    return result["metadatas"][0].get("paper_id", "")
            except Exception as e:
                logger.debug("  标题查询异常: %s", e)

    return None


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

    return removed


# ═══════════════════════════════════════════════════════════════
#  主流程
# ═══════════════════════════════════════════════════════════════

def main():
    import argparse

    # 延迟导入（知识库环境中的依赖）
    import chromadb
    from sentence_transformers import SentenceTransformer

    # 知识库模块
    sys.path.insert(0, str(SCRIPTS_DIR))
    from utils import (
        CHUNK_MAX_WORDS,
        chunk_text,
        clean_text,
        compute_paper_id_from_doi,
        extract_text_from_markdown,
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
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="详细日志输出")
    args = parser.parse_args()

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

    global MINERU_DIR, MINERU_PYTHON, MINERU_SCRIPT
    MINERU_DIR = _md
    MINERU_PYTHON = (_md / ".venv" / "Scripts" / "python.exe") if _md else None
    # MINERU_SCRIPT 不受此影响，始终在项目 scripts/ 下

    zotero_dir: Path = args.zotero_dir
    zotero_storage = zotero_dir / "storage"
    dry_run = args.dry_run
    _start_time = time.time()

    logger.info("=" * 55)
    logger.info("Zotero 同步开始")
    logger.info("  Zotero 目录: %s", zotero_dir)
    logger.info("  知识库根目录: %s", REPO_ROOT)
    if MINERU_PYTHON:
        logger.info("  MinerU: %s", MINERU_PYTHON)
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
        checkpoint = {"last_item_id": 0, "last_version": 0}
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
    )
    logger.info("待处理的论文数: %d", len(items))

    if not items:
        logger.info("没有新论文需要同步")
        zotero_conn.close()
        return

    # ── 加载模型 ───────────────────────────────────────────
    logger.info("加载嵌入模型...")
    model = load_bi_encoder()

    # ── 连接 ChromaDB ──────────────────────────────────────
    logger.info("连接向量数据库...")
    client = chromadb.PersistentClient(str(CHROMA_DIR))
    collection = get_or_create_collection(client)

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
        "skipped_no_pdf": 0,
        "skipped_dup": 0,
        "skipped_extract_fail": 0,
        "imported": 0,
        "errors": [],
    }

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
        if existing_id and not args.full_rescan:
            logger.info("  -> 跳过: 已在知识库中 (paper_id=%s)", existing_id)
            stats["skipped_dup"] += 1
            continue

        # 3. 获取附件
        attachment_key = get_attachment_info(cursor, item_id)
        if not attachment_key:
            logger.info("  -> 跳过: 无 PDF 附件")
            stats["skipped_no_pdf"] += 1
            continue

        pdf_path = resolve_pdf_path(zotero_storage, attachment_key)
        if not pdf_path:
            logger.info("  -> 跳过: storage 中未找到 PDF (key=%s)", attachment_key)
            stats["skipped_no_pdf"] += 1
            continue

        logger.info("  PDF: %s", pdf_path.name)

        if dry_run:
            logger.info("  [dry-run] 将提取并导入此论文")
            continue

        # 4. MinerU 提取文本
        mineru_result = extract_pdf_with_mineru(pdf_path, mineru_output_dir)
        if not mineru_result:
            logger.info("  -> 跳过: MinerU 提取失败")
            stats["skipped_extract_fail"] += 1
            continue

        pdf_text = mineru_result.get("text", "")
        if not pdf_text or len(pdf_text.strip()) < 20:
            logger.info("  -> 跳过: 提取文本过短")
            stats["skipped_extract_fail"] += 1
            continue

        logger.debug("  提取文本长度: %d 字符", len(pdf_text))

        # 5. 处理元数据
        doi_norm = item["doi"].strip().lower().rstrip(".") if item["doi"] else ""
        authors = format_authors(cursor, item_id)
        collections = lookup_collections(cursor, item_id, collection_map)
        year = extract_year_from_date(item["date"])

        # 6. 构建 enriched 全文（将 Zotero 的 abstractNote 前置）
        full_text = pdf_text
        if item["abstract_note"]:
            # 如果 MinerU 提取的文本中也包含摘要，不必再前置
            # 但仍确保 metadata 中有摘要
            pass

        # 7. 分块
        chunks = chunk_text(full_text, title=item["title"], max_words=CHUNK_MAX_WORDS)
        if not chunks:
            logger.info("  -> 跳过: 分块结果为空")
            stats["skipped_extract_fail"] += 1
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
                "filename": pdf_path.name,
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
            embeddings = model.encode(
                documents,
                batch_size=32,
                normalize_embeddings=True,
                show_progress_bar=False,
            )
        except Exception as e:
            logger.warning("  嵌入失败: %s", e)
            stats["errors"].append((item["title"], str(e)))
            continue

        # 10. 写入 ChromaDB
        try:
            # 先清除该 paper_id 的旧数据
            old_ids = collection.get(
                include=[],
                where={"paper_id": paper_id},
            )["ids"]
            if old_ids:
                collection.delete(ids=old_ids)
                logger.debug("  已清理旧 chunks: %d 个", len(old_ids))

            # 批量写入
            batch_size = 50
            inserted_ids = []
            for i in range(0, len(ids), batch_size):
                batch_end = i + batch_size
                collection.add(
                    ids=ids[i:batch_end],
                    embeddings=embeddings[i:batch_end].tolist(),
                    documents=documents[i:batch_end],
                    metadatas=metadatas[i:batch_end],
                )
                inserted_ids.extend(ids[i:batch_end])

        except Exception as e:
            logger.warning("  写入 ChromaDB 失败: %s", e)
            try:
                collection.delete(ids=inserted_ids)
                logger.info("  已回滚 %d 个 chunks", len(inserted_ids))
            except Exception:
                pass
            stats["errors"].append((item["title"], str(e)))
            continue

        stats["imported"] += 1
        logger.info("  ✓ 导入成功 (%d chunks)", len(chunks))

    # ── 删除同步 ───────────────────────────────────────────
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
        max_item_id = max((it["item_id"] for it in items), default=checkpoint["last_item_id"])
        save_checkpoint({
            "last_item_id": max_item_id,
            "last_version": current_version,
        })
        logger.info("检查点已保存 (item_id=%d, version=%d)", max_item_id, current_version)

    # ── 重建 FTS 索引 ─────────────────────────────────────
    if not dry_run and not args.skip_build_index and stats["imported"] > 0:
        logger.info("\n检测到新论文，重建文本搜索索引...")
        try:
            from build_index import main as build_index_main
            build_index_main()
        except Exception as e:
            logger.warning("文本索引重建失败: %s", e)
            logger.info("可稍后手动运行: python scripts/build_index.py")

    # ── 汇总 ──────────────────────────────────────────────
    elapsed = time.time() - _start_time

    logger.info("\n" + "=" * 55)
    logger.info("同步完成!")
    logger.info("  总计:            %d 篇", stats["total"])
    logger.info("  已导入:           %d 篇", stats["imported"])
    logger.info("  跳过（无 PDF）:   %d 篇", stats["skipped_no_pdf"])
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


if __name__ == "__main__":
    main()
