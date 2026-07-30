"""
论文知识库 — 构建文本搜索索引
============================
从 ChromaDB 提取所有论文的摘要和全文，构建 SQLite 文本索引
（FTS5 + 标准表），供 quick_search.py 快速检索。

用法: python scripts/build_index.py
"""

import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

from tqdm import tqdm
from abstract_extractor import extract_abstract, generate_summary
from models import get_or_create_chroma_collection
from paths import COLLECTION_NAME, INDEX_DB, SCRIPTS_DIR
from utils import ensure_utf8_stdout, setup_logging

ensure_utf8_stdout()

# 确保能找到 scripts/ 下的模块
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

INDEX_DB.parent.mkdir(parents=True, exist_ok=True)
LOG_FILE = INDEX_DB.parent / "build_index.log"
logger = setup_logging("build_index", LOG_FILE, mode="w")


# ── 数据库初始化 ─────────────────────────────────────────────

def init_db(conn: sqlite3.Connection):
    """创建 papers 表和 FTS5 虚拟表。"""
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("PRAGMA synchronous=OFF")  # 构建期间允许更快写入

    conn.execute("DROP TABLE IF EXISTS papers")
    conn.execute("DROP TABLE IF EXISTS papers_fts")

    conn.execute("""
        CREATE TABLE papers (
            paper_id    TEXT PRIMARY KEY,
            title       TEXT NOT NULL,
            filename    TEXT NOT NULL,
            abstract    TEXT DEFAULT '',
            summary     TEXT DEFAULT '',
            abstract_len INTEGER DEFAULT 0,
            has_abstract INTEGER DEFAULT 0,
            source      TEXT DEFAULT 'papers',
            doi         TEXT DEFAULT '',
            authors     TEXT DEFAULT '',
            journal     TEXT DEFAULT '',
            collections TEXT DEFAULT '',
            zotero_key  TEXT DEFAULT ''
        )
    """)

    conn.execute("""
        CREATE VIRTUAL TABLE papers_fts USING fts5(
            title, abstract, summary,
            tokenize='trigram'
        )
    """)

    # 普通索引加速 LIKE 前缀匹配
    conn.execute("CREATE INDEX idx_papers_title ON papers(title)")
    conn.execute("CREATE INDEX idx_papers_abstract ON papers(abstract)")
    conn.execute("CREATE INDEX idx_papers_summary ON papers(summary)")

    conn.commit()
    logger.info("数据库表已初始化")


def insert_paper(conn: sqlite3.Connection, paper_id: str, title: str,
                 filename: str, abstract: str, summary: str,
                 has_abstract: bool, **extra):
    """插入一篇论文及其 FTS 索引。

    Args:
        extra: 可选扩展字段 (source, doi, authors, journal, collections, zotero_key)
    """
    conn.execute(
        """INSERT INTO papers (paper_id, title, filename, abstract, summary,
           abstract_len, has_abstract,
           source, doi, authors, journal, collections, zotero_key
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (paper_id, title, filename, abstract, summary,
         len(abstract), 1 if has_abstract else 0,
         extra.get("source", "papers"),
         extra.get("doi", ""),
         extra.get("authors", ""),
         extra.get("journal", ""),
         extra.get("collections", ""),
         extra.get("zotero_key", ""),
         )
    )
    conn.execute(
        """INSERT INTO papers_fts(rowid, title, abstract, summary)
           VALUES (last_insert_rowid(), ?, ?, ?)""",
        (title, abstract, summary),
    )


# ── 主流程 ───────────────────────────────────────────────────

def main():
    start_time = time.time()

    # 连接 ChromaDB
    logger.info("连接 ChromaDB...")
    try:
        collection = get_or_create_chroma_collection()
    except Exception:
        logger.error("ChromaDB 连接失败，请确认数据库存在", COLLECTION_NAME)
        sys.exit(1)

    total_chunks = collection.count()
    if total_chunks == 0:
        logger.error("ChromaDB 中没有数据，请先运行 ingest.py 或 sync_zotero.py 导入论文")
        sys.exit(1)
    logger.info("ChromaDB 中共有 %d 个 chunks", total_chunks)

    # 分页加载所有 chunks（每次 2000 个）
    logger.info("加载所有 chunks（按 paper_id 分组）...")
    papers_data: dict[str, dict] = {}  # paper_id -> {title, filename, chunks, extra_meta}

    offset = 0
    batch_size = 2000
    with tqdm(total=total_chunks, desc="加载 chunks", unit="块") as pbar:
        while offset < total_chunks:
            result = collection.get(
                include=["documents", "metadatas"],
                limit=batch_size,
                offset=offset,
            )
            if not result["ids"]:
                break

            for doc_id, doc, meta in zip(result["ids"], result["documents"],
                                         result["metadatas"]):
                pid = meta.get("paper_id", doc_id.split("#")[0])
                if pid not in papers_data:
                    papers_data[pid] = {
                        "title": meta.get("title", ""),
                        "filename": meta.get("filename", ""),
                        "chunks": [],
                        # 从第一个 chunk 的 metadata 中提取扩展字段
                        "extra": {
                            "source": meta.get("source", "papers"),
                            "doi": meta.get("doi", ""),
                            "authors": meta.get("authors", ""),
                            "journal": meta.get("journal", ""),
                            "collections": meta.get("collections", ""),
                            "zotero_key": meta.get("zotero_key", ""),
                        },
                    }
                papers_data[pid]["chunks"].append(
                    (meta.get("chunk_index", 0), doc)
                )

            offset += len(result["ids"])
            pbar.update(len(result["ids"]))

    logger.info("共加载 %d 篇独立论文", len(papers_data))

    # 在同一目录构建临时数据库，成功后原子替换现有索引。
    logger.info("初始化文本索引数据库 %s", INDEX_DB)
    with tempfile.NamedTemporaryFile(
        prefix="index-",
        suffix=".db.tmp",
        dir=INDEX_DB.parent,
        delete=False,
    ) as temp_handle:
        temp_path = Path(temp_handle.name)
    conn = sqlite3.connect(str(temp_path))
    build_succeeded = False

    try:
        init_db(conn)

        # 逐篇处理
        stats = {"with_abstract": 0, "fallback": 0, "empty": 0, "total": 0}
        insert_batch: list[tuple] = []

        for paper_id, data in tqdm(papers_data.items(), desc="提取摘要", unit="篇"):
            # 按 chunk_index 排序并拼接全文
            data["chunks"].sort(key=lambda x: x[0])
            full_text = " ".join(chunk[1] for chunk in data["chunks"])

            # 提取摘要
            abstract, found_marker = extract_abstract(full_text)

            # 生成摘要
            summary = generate_summary(abstract, full_text)

            insert_batch.append((
                paper_id,
                data["title"],
                data["filename"],
                abstract,
                summary,
                1 if found_marker else 0,
                data.get("extra", {"source": "papers"}),
            ))

            stats["total"] += 1
            if found_marker:
                stats["with_abstract"] += 1
            elif abstract:
                stats["fallback"] += 1
            else:
                stats["empty"] += 1

            # 每 50 篇批量写入
            if len(insert_batch) >= 50:
                for item in insert_batch:
                    paper_id, title, filename, abstract, summary, has_abstract, extra = item
                    insert_paper(conn, paper_id, title, filename, abstract, summary, has_abstract, **extra)
                conn.commit()
                insert_batch.clear()

        # 写入剩余
        if insert_batch:
            for item in insert_batch:
                paper_id, title, filename, abstract, summary, has_abstract, extra = item
                insert_paper(conn, paper_id, title, filename, abstract, summary, has_abstract, **extra)
            conn.commit()

        # 验证
        count = conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
        fts_count = conn.execute("SELECT COUNT(*) FROM papers_fts").fetchone()[0]

        elapsed = time.time() - start_time
        logger.info("\n" + "=" * 50)
        logger.info("索引构建完成!")
        logger.info("  论文总数:        %d", stats["total"])
        logger.info("  检测到摘要标记:  %d (%.1f%%)",
                     stats["with_abstract"],
                     100 * stats["with_abstract"] / max(stats["total"], 1))
        logger.info("  使用了回退策略:  %d", stats["fallback"])
        logger.info("  无摘要文本:      %d", stats["empty"])
        logger.info("  SQLite 记录数:   %d (FTS: %d)", count, fts_count)
        logger.info("  数据库文件:      %s", INDEX_DB)
        logger.info("  耗时:           %.1f 秒", elapsed)
        build_succeeded = True

    finally:
        conn.close()
        if build_succeeded:
            os.replace(temp_path, INDEX_DB)
        else:
            temp_path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
