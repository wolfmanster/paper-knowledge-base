"""
论文知识库 — 快速文本搜索
==========================
在标题、摘要、AI 摘要中进行关键词/文本搜索（LIKE + FTS5 混合）。

用法: python scripts/quick_search.py "关键字" [top_k]

输出: JSON 数组，每个结果包含:
  - title: 论文标题
  - filename: 原始文件名
  - abstract_preview: 摘要前 300 字符
  - summary: AI 摘要（提取式）
  - match_type: 匹配字段（title/abstract/summary/title+abstract 等）
  - score: 相关性评分（0-10）
"""

import json
import re
import sqlite3
import sys

from paths import INDEX_DB
from utils import ensure_utf8_stdout, has_chinese

ensure_utf8_stdout()


def _determine_match_type(
    query_lower: str,
    title: str,
    abstract: str,
    summary: str,
    use_word_boundary: bool = False,
) -> str:
    """判断查询命中了哪些字段。

    Args:
        use_word_boundary: 启用单词边界匹配，避免短查询误匹配子串。
    """
    if use_word_boundary:
        pattern = re.compile(rf"\b{re.escape(query_lower)}\b")
        matched = []
        if pattern.search(title.lower()):
            matched.append("title")
        if pattern.search(abstract.lower()):
            matched.append("abstract")
        if pattern.search(summary.lower()):
            matched.append("summary")
        return "+".join(matched) if matched else "unknown"

    matched = []
    if query_lower in title.lower():
        matched.append("title")
    if query_lower in abstract.lower():
        matched.append("abstract")
    if query_lower in summary.lower():
        matched.append("summary")
    return "+".join(matched) if matched else "unknown"


def _highlight_preview(text: str, query: str, max_len: int = 300) -> str:
    """截取包含查询词的文本片段，并在匹配处添加 ** 标记。"""
    if not text or not query:
        return text[:max_len] if text else ""

    idx = text.lower().find(query.lower())
    if idx == -1:
        return text[:max_len]

    # 从匹配位置向前扩展，取上下文
    start = max(0, idx - 80)
    end = min(len(text), idx + len(query) + 200)
    snippet = text[start:end]

    # 标记匹配词
    pattern = re.compile(re.escape(query), re.IGNORECASE)
    snippet = pattern.sub(lambda m: f"**{m.group()}**", snippet)

    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(text) else ""
    return f"{prefix}{snippet}{suffix}"


def search(query: str, top_k: int = 10) -> list:
    """在标题、摘要和 AI 摘要中进行快速关键词搜索。

    Args:
        query: 搜索关键词
        top_k: 返回结果数量

    Returns:
        匹配论文列表（按得分降序排列）
    """
    if not query or not query.strip():
        return []

    query = query.strip()

    if not INDEX_DB.exists():
        return [{"error": f"索引数据库不存在: {INDEX_DB}，请先运行 python scripts/build_index.py"}]

    conn = sqlite3.connect(str(INDEX_DB))
    conn.row_factory = sqlite3.Row

    try:
        query_lower = query.lower()
        like_pattern = f"%{query}%"
        results = []

        # ── 第一阶段：LIKE 搜索（主搜索，支持任意长度查询）──
        like_rows = conn.execute(
            """SELECT paper_id, title, filename, abstract, summary, has_abstract,
                      CASE
                          WHEN title LIKE ? COLLATE NOCASE THEN 10
                          WHEN abstract LIKE ? COLLATE NOCASE THEN 7
                          WHEN summary LIKE ? COLLATE NOCASE THEN 5
                          ELSE 0
                      END AS base_score
               FROM papers
               WHERE title LIKE ? COLLATE NOCASE
                  OR abstract LIKE ? COLLATE NOCASE
                  OR summary LIKE ? COLLATE NOCASE
               ORDER BY base_score DESC, title ASC
               LIMIT ?""",
            (like_pattern, like_pattern, like_pattern,
             like_pattern, like_pattern, like_pattern,
             top_k + 10),
        ).fetchall()

        results = [dict(r) for r in like_rows]

        # ── 第二阶段：FTS5 排名细化（仅 ≥3 字符查询）──
        if len(query) >= 3:
            try:
                fts_rows = conn.execute(
                    """SELECT p.paper_id, p.title, p.filename, p.abstract, p.summary,
                              p.has_abstract, rank + 1 AS fts_score
                       FROM papers_fts f
                       JOIN papers p ON p.rowid = f.rowid
                       WHERE papers_fts MATCH ?
                       ORDER BY rank
                       LIMIT ?""",
                    (query, top_k + 10),
                ).fetchall()

                if fts_rows:
                    fts_dict = {r["paper_id"]: dict(r) for r in fts_rows}

                    # 提升 FTS5 匹配结果的排名
                    for r in results:
                        pid = r["paper_id"]
                        if pid in fts_dict:
                            r["base_score"] = max(r["base_score"], 15 - fts_dict[pid].get("fts_score", 10))

                    # 添加 ONLY 在 FTS5 中出现的结果
                    existing_ids = {r["paper_id"] for r in results}
                    for r in fts_rows:
                        if r["paper_id"] not in existing_ids:
                            d = dict(r)
                            d["base_score"] = 15 - d.get("fts_score", 10)
                            results.append(d)

                    # 重新排序
                    results.sort(key=lambda x: x.get("base_score", 0), reverse=True)
            except sqlite3.OperationalError:
                # FTS5 解析错误（特殊字符），忽略，使用 LIKE 结果
                pass

        # ── 格式化输出 ──
        output = []
        for r in results[:top_k]:
            title = r.get("title", "")
            abstract = r.get("abstract", "")
            summary = r.get("summary", "")

            match_type = _determine_match_type(query_lower, title, abstract, summary)

            output.append({
                "title": title,
                "filename": r.get("filename", ""),
                "abstract_preview": _highlight_preview(abstract, query, 300),
                "summary": summary,
                "match_type": match_type,
                "score": round(float(r.get("base_score", 0)), 1),
            })

        # ── 第三阶段：短英文查询的单词边界过滤 ──
        # "PEC" 不应匹配 "especially"。对 <=4 字符的纯英文查询执行 \b 过滤。
        if len(query) <= 4 and not has_chinese(query) and query.isascii():
            word_pattern = re.compile(rf"\b{re.escape(query)}\b", re.IGNORECASE)
            exact_matches: list[dict] = []
            substring_only: list[dict] = []

            for r in output:
                title_text = r.get("title", "")
                abstract_text = r.get("abstract_preview", "").replace("**", "")
                summary_text = r.get("summary", "")
                if (
                    word_pattern.search(title_text)
                    or word_pattern.search(abstract_text)
                    or word_pattern.search(summary_text)
                ):
                    exact_matches.append(r)
                else:
                    substring_only.append(r)

            if exact_matches:
                output = exact_matches + substring_only
                if len(exact_matches) >= top_k:
                    output = output[:top_k]

            # 更新 match_type 为单词边界结果
            for r in output:
                r["match_type"] = _determine_match_type(
                    query.lower(),
                    r.get("title", ""),
                    r.get("abstract_preview", "").replace("**", ""),
                    r.get("summary", ""),
                    use_word_boundary=True,
                )

        return output

    finally:
        conn.close()


if __name__ == "__main__":
    # ── Schema introspection ──
    if "--schema" in sys.argv or "--list-tables" in sys.argv:
        if not INDEX_DB.exists():
            print(json.dumps(
                {"error": f"索引数据库不存在: {INDEX_DB}，请先运行 python scripts/build_index.py"},
                ensure_ascii=False,
                indent=2,
            ))
            sys.exit(1)
        conn = sqlite3.connect(str(INDEX_DB))
        tables: dict[str, dict[str, str | None]] = {}
        for row in conn.execute(
            "SELECT name, type, sql FROM sqlite_master ORDER BY name"
        ).fetchall():
            tables[row[0]] = {"type": row[1], "sql": row[2]}
        conn.close()
        print(json.dumps({
            "database": str(INDEX_DB),
            "tables": tables,
        }, ensure_ascii=False, indent=2))
        sys.exit(0)

    if len(sys.argv) < 2:
        print(json.dumps(
            {"error": "用法: python scripts/quick_search.py <关键字> [top_k]"},
            ensure_ascii=False,
            indent=2,
        ))
        sys.exit(1)

    query = sys.argv[1]
    try:
        top_k = int(sys.argv[2]) if len(sys.argv) > 2 else 10
    except ValueError:
        top_k = 10

    results = search(query, top_k)
    print(json.dumps(results, ensure_ascii=False, indent=2))
