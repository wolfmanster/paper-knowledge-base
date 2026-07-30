"""Zotero 本地 SQLite 数据库读取与查询。

从 sync_zotero.py 拆分而来。包含论文项、附件、分类、作者、版本的查询。
仅依赖标准库与 logging；不依赖 ChromaDB / MinerU / sentence_transformers。
"""

from __future__ import annotations

import atexit
import logging
import sqlite3
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# 在模块层级维护 Zotero 连接引用，供 atexit 清理
_ZOTERO_CONN: sqlite3.Connection | None = None


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
