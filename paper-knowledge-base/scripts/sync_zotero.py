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

import atexit
import errno
import hashlib
import json
import logging
import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import BinaryIO

from utils import (
    CHROMA_DIR,
    SCRIPTS_DIR,
    ensure_utf8_stdout,
    get_or_create_chroma_collection,
    get_version,
    load_bi_encoder,
    setup_logging,
)

# ── 路径 ─────────────────────────────────────────────────────
REPO_ROOT = SCRIPTS_DIR.parent

CHECKPOINT_FILE = REPO_ROOT / "kb" / "zotero_checkpoint.json"
LOG_FILE = REPO_ROOT / "kb" / "sync_zotero.log"
SYNC_LOCK_FILE = REPO_ROOT / "kb" / "sync_zotero.lock"
DEFAULT_MINERU_TIMEOUT_SECONDS = 24 * 60 * 60

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


class SyncProcessLock:
    """Cross-platform advisory lock that serializes all sync processes."""

    def __init__(self, path: Path):
        self.path = path
        self._handle: BinaryIO | None = None

    @staticmethod
    def _try_lock(handle: BinaryIO) -> None:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _unlock(handle: BinaryIO) -> None:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def acquire(self, *, blocking: bool = True, poll_seconds: float = 1.0) -> bool:
        if self._handle is not None:
            return True

        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b", buffering=0)
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"\0")

        waiting_logged = False
        while True:
            try:
                self._try_lock(handle)
                self._handle = handle
                return True
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    handle.close()
                    raise
                if not blocking:
                    handle.close()
                    return False
                if not waiting_logged:
                    logger.info("另一个 Zotero 同步正在运行，等待其完成...")
                    waiting_logged = True
                time.sleep(poll_seconds)

    def release(self) -> None:
        if self._handle is None:
            return
        handle = self._handle
        self._handle = None
        try:
            self._unlock(handle)
        finally:
            handle.close()

    def __enter__(self) -> SyncProcessLock:
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.release()

# ── 日志 ─────────────────────────────────────────────────────
CHROMA_DIR.mkdir(parents=True, exist_ok=True)

ensure_utf8_stdout()
logger = setup_logging("sync_zotero", LOG_FILE)


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
    pending_item_ids: list[int] | None = None,
) -> list[dict]:
    """获取 Zotero 中所有可检索的论文项及其元数据。

    可检索类型: journalArticle, conferencePaper, thesis, preprint, computerProgram
    排除: 已被删除的项

    Args:
        since_item_id: 仅返回 itemID > 此值的项（增量同步）
        since_version: 仅返回 version > 此值的项（增量同步）
        pending_item_ids: 无论版本如何都重新返回的失败项目 ID

    Returns:
        论文项字典列表，每项包含 title, abstractNote, doi, 等
    """
    pending_ids = sorted({int(item_id) for item_id in (pending_item_ids or [])})
    pending_clause = ""
    params: list[int] = [since_item_id, since_version]
    if pending_ids:
        placeholders = ",".join("?" for _ in pending_ids)
        pending_clause = f" OR i.itemID IN ({placeholders})"
        params.extend(pending_ids)

    rows = cursor.execute(
        f"""
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
          AND (i.itemID > ? OR i.version > ?{pending_clause})
        ORDER BY i.itemID
        """,
        params,
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


def get_attachment_info(cursor, parent_item_id: int) -> dict | None:
    """获取论文的附件信息。

    优先返回 PDF 附件；如无 PDF，则查找 .docx 附件。

    Args:
        parent_item_id: 父论文项 ID

    Returns:
        {"key": items.key, "content_type": "application/pdf"|"application/vnd.openxmlformats-officedocument.wordprocessingml.document"}
        如果没有附件则返回 None
    """
    row = cursor.execute(
        """
        SELECT i.key, ia.contentType
        FROM itemAttachments ia
        JOIN items i ON ia.itemID = i.itemID
        WHERE ia.parentItemID = ?
          AND ia.linkMode = 0
          AND ia.contentType IN ('application/pdf', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document')
        ORDER BY CASE ia.contentType
          WHEN 'application/pdf' THEN 1
          ELSE 2
        END
        LIMIT 1
        """,
        (parent_item_id,),
    ).fetchone()

    if row:
        return {"key": row["key"], "content_type": row["contentType"]}
    return None


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


def resolve_mineru_python(mineru_dir: Path) -> Path:
    """Locate the MinerU virtualenv interpreter on Windows or POSIX systems."""
    candidates = (
        mineru_dir / ".venv" / "Scripts" / "python.exe",
        mineru_dir / ".venv" / "bin" / "python",
        mineru_dir / ".venv" / "bin" / "python3",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0] if os.name == "nt" else candidates[1]


# ═══════════════════════════════════════════════════════════════
#  文档提取（MinerU subprocess，支持 PDF/Word/图片）
# ═══════════════════════════════════════════════════════════════

def resolve_attachment_path(
    zotero_storage: Path,
    attachment_key: str,
    content_type: str,
    max_size_mb: int = 500,
) -> Path | None:
    """在 Zotero storage/ 目录中查找附件文件。

    根据 content_type 查找对应后缀的文件（.pdf 或 .docx）。
    跳过超过 max_size_mb 的超大文件。

    Args:
        zotero_storage: 指向 Zotero 的 storage/ 目录
        attachment_key: 附件项的 items.key
        content_type: MIME 类型（决定要查找的文件后缀）
        max_size_mb: 文件大小上限（MB），超过返回 None

    Returns:
        第一个找到的匹配文件路径，未找到返回 None
    """
    storage_dir = zotero_storage / attachment_key
    if not storage_dir.is_dir():
        logger.debug("  storage 目录不存在: %s", storage_dir)
        return None

    if content_type == "application/pdf":
        ext_pattern = "*.pdf"
        type_label = "PDF"
    elif content_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
        ext_pattern = "*.docx"
        type_label = "Word"
    else:
        logger.debug("  不支持的附件类型: %s", content_type)
        return None

    files = sorted(storage_dir.glob(ext_pattern))
    if not files:
        logger.debug("  目录中无 %s 文件: %s", type_label, storage_dir)
        return None

    file_path = files[0]
    if len(files) > 1:
        logger.debug("  storage 目录中有多个 %s，取第一个: %s", type_label, file_path.name)

    # 超大文件检查
    max_bytes = max_size_mb * 1024 * 1024
    try:
        file_stat = file_path.stat()
        if file_stat.st_size > max_bytes:
            logger.warning("  %s 文件过大 (>%dMB)，跳过: %s (%.1fMB)",
                           type_label, max_size_mb, file_path.name, file_stat.st_size / (1024*1024))
            return None
    except OSError as e:
        logger.warning("  无法读取文件大小: %s — %s", file_path.name, e)
        return None

    return file_path


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    """Forcefully terminate a MinerU process and all descendants."""
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            check=False,
        )
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        process.kill()


def run_captured_process(
    command: list[str],
    *,
    timeout_seconds: float,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a captured subprocess with a timeout that also kills descendants."""
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be greater than zero")

    process_kwargs: dict = {}
    if os.name == "nt":
        process_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        process_kwargs["start_new_session"] = True

    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        **process_kwargs,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        _terminate_process_tree(process)
        process.communicate()
        raise
    return subprocess.CompletedProcess(
        command,
        process.returncode,
        stdout=stdout,
        stderr=stderr,
    )


def extract_with_mineru(
    file_path: Path,
    output_dir: Path,
    *,
    lang: str = "en",
    max_pages: int = 0,
    timeout_seconds: float = DEFAULT_MINERU_TIMEOUT_SECONDS,
) -> dict | None:
    """通过 MinerU subprocess 提取文档文本。

    支持 PDF 和 Word (.docx) 文件，调用 mineru_extract.py（在 MinerU venv 中运行），
    读取 stdout 中的 JSON 结果。

    Args:
        file_path: 文件路径（.pdf 或 .docx）
        output_dir: MinerU 输出目录
        lang: OCR 语言（默认 en）
        max_pages: 最多处理页数；0 表示全部页面（Word 文件忽略此参数）
        timeout_seconds: 单篇 MinerU 最长运行秒数（默认 24 小时）

    Returns:
        {"text": "pure text", "markdown": "raw md"}，失败返回 None
    """
    if MINERU_PYTHON is None:
        logger.error("  MinerU 路径未配置，跳过文档提取")
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

        proc = run_captured_process(
            [
                str(MINERU_PYTHON),
                str(MINERU_SCRIPT),
                str(file_path),
                str(output_dir),
                "--lang", lang,
                "--max_pages", str(max_pages),
            ],
            timeout_seconds=timeout_seconds,
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
        logger.warning(
            "  MinerU 超过 %.1f 小时，已终止进程树并保留到重试队列: %s",
            timeout_seconds / 3600,
            file_path.name,
        )
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

# load_bi_encoder / get_or_create_collection: 从 utils 导入


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
        _norm_cache = {title: title_norm}  # metadata title -> normalized title 缓存
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
                    mt = metadata.get("title", "")
                    if mt not in _norm_cache:
                        _norm_cache[mt] = _normalize_title(mt)
                    if _norm_cache[mt] == title_norm:
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


# ═══════════════════════════════════════════════════════════════
#  主流程
# ═══════════════════════════════════════════════════════════════

def _main_unlocked():
    # FIXME: 约 400 行，应拆分出 _sync_items(), _sync_deletions(), _rebuild_indexes()
    import argparse

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
        print(f"paper-knowledge-base {get_version(REPO_ROOT.parent)}")
        sys.exit(0)

    # 延迟导入（知识库环境中的依赖）
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

    global MINERU_DIR, MINERU_PYTHON
    MINERU_DIR = _md
    MINERU_PYTHON = resolve_mineru_python(_md) if _md else None
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
            raw = f"zotero:{item['key']}:{item_id}".encode()
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
