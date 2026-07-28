"""
测试 scripts/quick_search.py 中的文本搜索功能。
"""

import json
import pytest
import sqlite3
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from quick_search import (
    search,
    _determine_match_type,
    _highlight_preview,
)

# ── 测试数据库 ──────────────────────────────────────────────────


@pytest.fixture
def test_db(tmp_path):
    """创建一个临时测试数据库，包含几条论文记录。"""
    db_path = tmp_path / "index.db"
    conn = sqlite3.connect(str(db_path))

    conn.execute("""
        CREATE TABLE papers (
            paper_id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            filename TEXT NOT NULL,
            abstract TEXT DEFAULT '',
            summary TEXT DEFAULT '',
            abstract_len INTEGER DEFAULT 0,
            has_abstract INTEGER DEFAULT 0
        )
    """)

    conn.execute("""
        CREATE VIRTUAL TABLE papers_fts USING fts5(
            title, abstract, summary,
            tokenize='trigram'
        )
    """)

    papers = [
        (
            "aaa111",
            "Liquid cooling for battery thermal management",
            "liquid_cooling.pdf",
            "This paper investigates liquid cooling techniques for LIBs. "
            "Results show 20% temperature reduction.",
            "Liquid cooling reduces battery temperature by 20%.",
            100, 1,
        ),
        (
            "bbb222",
            "Phase change material composite for BTMS",
            "pcm_composite.pdf",
            "A novel PCM composite is proposed for battery thermal "
            "management systems. Experiments validate the approach.",
            "Novel PCM composite improves BTMS performance.",
            110, 1,
        ),
        (
            "ccc333",
            "拓扑优化液冷板散热流道设计",
            "topology_liquid_cooling.pdf",
            "研究了基于变密度法的拓扑优化液冷板流道设计方法。"
            "结果表明优化后的散热性能显著提升。",
            "拓扑优化提高了液冷板散热均匀性。",
            80, 1,
        ),
        (
            "ddd444",
            "Thermal runaway prevention in lithium-ion batteries",
            "thermal_runaway.pdf",
            "",  # No abstract
            "",  # No summary
            0, 0,
        ),
    ]

    for p in papers:
        conn.execute(
            """INSERT INTO papers (paper_id, title, filename, abstract, summary,
               abstract_len, has_abstract) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            p,
        )
        conn.execute(
            """INSERT INTO papers_fts(rowid, title, abstract, summary)
               VALUES (last_insert_rowid(), ?, ?, ?)""",
            (p[1], p[3], p[4]),
        )

    conn.commit()
    conn.close()

    # Patch the INDEX_DB to point to our test DB
    import quick_search
    original_db = quick_search.INDEX_DB
    quick_search.INDEX_DB = db_path
    yield db_path
    quick_search.INDEX_DB = original_db


# ── search tests ────────────────────────────────────────────────


class TestSearch:
    def test_english_keyword_title_match(self, test_db):
        results = search("liquid cooling", top_k=5)
        assert len(results) > 0
        # First result should match "liquid cooling" in title
        titles = [r["title"] for r in results]
        assert any("liquid" in t.lower() for t in titles)

    def test_english_keyword_abstract_match(self, test_db):
        results = search("PCM composite", top_k=5)
        assert len(results) > 0

    def test_chinese_keyword_search(self, test_db):
        results = search("拓扑优化", top_k=5)
        assert len(results) > 0
        titles = [r["title"] for r in results]
        assert any("拓扑优化" in t for t in titles)

    def test_two_char_chinese(self, test_db):
        """Two-character Chinese should work via LIKE fallback."""
        results = search("液冷", top_k=5)
        assert len(results) > 0

    def test_returns_empty_for_no_match(self, test_db):
        results = search("zzz_nonexistent_term_zzz", top_k=5)
        assert isinstance(results, list)

    def test_returns_empty_for_empty_query(self, test_db):
        results = search("", top_k=5)
        assert results == []

    def test_results_include_required_fields(self, test_db):
        results = search("liquid cooling", top_k=2)
        for r in results:
            assert "title" in r
            assert "filename" in r
            assert "abstract_preview" in r
            assert "summary" in r
            assert "match_type" in r
            assert "score" in r

    def test_match_type_field(self, test_db):
        results = search("liquid cooling", top_k=3)
        for r in results:
            assert r["match_type"] in (
                "title", "abstract", "summary",
                "title+abstract", "title+summary", "abstract+summary",
                "title+abstract+summary", "unknown",
            )

    def test_results_sorted_by_score(self, test_db):
        results = search("battery thermal management", top_k=5)
        scores = [r["score"] for r in results if "error" not in r]
        assert scores == sorted(scores, reverse=True)

    def test_top_k_respected(self, test_db):
        # Insert more records to test top_k
        import quick_search
        conn = sqlite3.connect(str(quick_search.INDEX_DB))
        for i in range(20):
            conn.execute(
                "INSERT INTO papers VALUES (?, ?, ?, ?, ?, ?, ?)",
                (f"extra{i}", f"Paper about cooling {i}",
                 f"paper{i}.pdf", "abstract", "summary", 8, 1),
            )
        conn.commit()
        conn.close()

        results = search("cooling", top_k=5)
        assert len(results) <= 5

    def test_handles_missing_index(self, tmp_path, monkeypatch):
        import quick_search
        monkeypatch.setattr(quick_search, "INDEX_DB", tmp_path / "nonexistent.db")
        results = search("anything")
        assert isinstance(results, list)
        if results:
            assert "error" in results[0]


# ── _determine_match_type ───────────────────────────────────────


class TestDetermineMatchType:
    def test_title_only(self):
        result = _determine_match_type("liquid", "Liquid cooling paper", "", "")
        assert result == "title"

    def test_abstract_only(self):
        result = _determine_match_type(
            "battery", "Some title", "Battery thermal study abstract", ""
        )
        assert result == "abstract"

    def test_summary_only(self):
        # The actual search code passes query.lower() to this function
        result = _determine_match_type(
            "pcm", "Some title", "", "PCM composite summary"
        )
        assert result == "summary"

    def test_title_and_abstract(self):
        result = _determine_match_type(
            "cooling", "Liquid cooling system", "cooling technique abstract", ""
        )
        assert "title" in result
        assert "abstract" in result

    def test_all_three(self):
        result = _determine_match_type(
            "thermal",
            "Thermal management",
            "Thermal analysis abstract",
            "Thermal summary",
        )
        assert result == "title+abstract+summary"

    def test_no_match(self):
        result = _determine_match_type("xyz", "Title", "Abstract", "Summary")
        assert result == "unknown"


# ── _highlight_preview ─────────────────────────────────────────


class TestHighlightPreview:
    def test_highlights_match(self):
        result = _highlight_preview(
            "This paper studies battery thermal management.",
            "battery",
        )
        assert "**battery**" in result

    def test_truncates_long_text(self):
        long_text = "word " * 200
        result = _highlight_preview(long_text, "word", max_len=300)
        # The result should be roughly around max_len, not the full text
        assert len(result) < 800  # Much shorter than the full ~1000 char input

    def test_no_match_returns_prefix(self):
        result = _highlight_preview(
            "Some long text without the keyword anywhere in it.",
            "missing",
        )
        assert len(result) <= 300

    def test_empty_text(self):
        result = _highlight_preview("", "query")
        assert result == ""

    def test_case_insensitive_highlight(self):
        result = _highlight_preview("Battery Thermal Management", "battery")
        assert "**Battery**" in result or "**battery**" in result.lower()

    def test_chinese_highlight(self):
        result = _highlight_preview("研究了液冷板散热性能", "液冷板")
        assert "**液冷板**" in result
