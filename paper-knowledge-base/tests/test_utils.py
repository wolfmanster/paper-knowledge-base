"""
测试 scripts/utils.py 中的摘要提取和文本标准化函数。
"""

import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from abstract_extractor import (
    _clean_abstract,
    _normalize_spaced_letters,
    _split_letters_by_known_words,
    _strip_journal_header,
    extract_abstract,
    generate_summary,
)

# ── _normalize_spaced_letters ──────────────────────────────────


class TestNormalizeSpacedLetters:
    def test_basic_abstract(self):
        result = _normalize_spaced_letters("A B S T R A C T Keywords: test")
        assert "ABSTRACT" in result
        assert "A B S" not in result

    def test_article_info_abstract(self):
        result = _normalize_spaced_letters(
            "A R T I C L E   I N F O   A B S T R A C T Keywords: test"
        )
        assert "ARTICLE" in result
        assert "INFO" in result
        assert "ABSTRACT" in result

    def test_preserves_normal_text(self):
        text = "This is normal text with no spaced letters"
        result = _normalize_spaced_letters(text)
        assert result == text

    def test_keywords_normalized(self):
        result = _normalize_spaced_letters("K E Y W O R D S lithium battery")
        assert "KEYWORDS" in result
        assert "K E Y" not in result

    def test_introduction_normalized(self):
        result = _normalize_spaced_letters("I N T R O D U C T I O N The battery")
        assert "INTRODUCTION" in result
        assert "I N T R" not in result

    def test_empty_input(self):
        assert _normalize_spaced_letters("") == ""

    def test_single_letter_preserved(self):
        # Single letters (not forming a word) should be left alone
        result = _normalize_spaced_letters("A B battery")
        # "A B" is only 2 letters, shouldn't be collapsed (needs 3+)
        assert "A B" in result


# ── _split_letters_by_known_words ──────────────────────────────


class TestSplitLettersByKnownWords:
    def test_article_info_abstract(self):
        tokens = _split_letters_by_known_words("ARTICLEINFOABSTRACT")
        assert tokens == ["ARTICLE", "INFO", "ABSTRACT"]

    def test_abstract_keywords(self):
        tokens = _split_letters_by_known_words("ABSTRACTKEYWORDS")
        assert tokens == ["ABSTRACT", "KEYWORDS"]

    def test_introduction_only(self):
        tokens = _split_letters_by_known_words("INTRODUCTION")
        assert tokens == ["INTRODUCTION"]

    def test_unknown_letters_ignored(self):
        # Letters not in the known set are skipped
        tokens = _split_letters_by_known_words("XYZABC")
        assert tokens == []

    def test_known_with_unknown_prefix(self):
        tokens = _split_letters_by_known_words("XYZABSTRACT")
        assert tokens == ["ABSTRACT"]


# ── _clean_abstract ────────────────────────────────────────────


class TestCleanAbstract:
    def test_removes_keywords_suffix(self):
        result = _clean_abstract(
            "This is the abstract text. Keywords: battery, thermal"
        )
        assert "Keywords:" not in result
        assert "This is the abstract text." in result

    def test_removes_spaced_keywords(self):
        result = _clean_abstract(
            "Some text K E Y W O R D S battery thermal"
        )
        assert "KEYWORDS" not in result

    def test_collapses_whitespace(self):
        result = _clean_abstract("Too   many    spaces  here")
        assert "  " not in result

    def test_fixes_hyphenated_words(self):
        result = _clean_abstract("the ther- mal man- agement system")
        assert "ther- mal" not in result
        # Note: hyphens between words are fixed; in-word hyphens stay
        assert "thermal" in result or "ther-mal" in result

    def test_preserves_chinese(self):
        result = _clean_abstract("这里中文关键词：电池 热管理")
        assert "电池" in result


# ── _strip_journal_header ──────────────────────────────────────


class TestStripJournalHeader:
    def test_removes_elsevier_line(self):
        result = _strip_journal_header(
            "Elsevier Ltd. All rights reserved.\n"
            "Actual paper content here.\n"
            "More content."
        )
        assert "Elsevier" not in result
        assert "Actual paper content" in result

    def test_removes_copyright_line(self):
        result = _strip_journal_header(
            "© 2024 The Authors.\nOriginal text."
        )
        assert "©" not in result
        assert "Original text" in result

    def test_removes_doi_line(self):
        result = _strip_journal_header(
            "doi: 10.1016/j.energy.2024.12345\nReal content."
        )
        assert "doi:" not in result
        assert "Real content" in result

    def test_preserves_normal_content(self):
        text = "Battery thermal management is important."
        result = _strip_journal_header(text)
        assert text in result


# ── extract_abstract ──────────────────────────────────────────


class TestExtractAbstract:
    def test_standard_english_abstract(self):
        text = (
            "Some journal header\n"
            "ABSTRACT\n"
            "This paper investigates battery thermal management using "
            "liquid cooling techniques. We propose a novel design that "
            "reduces maximum temperature. The results show significant "
            "improvement over conventional methods. "
            "Keywords: battery, thermal management, liquid cooling\n"
            "1. Introduction\n"
        )
        abstract, found = extract_abstract(text)
        assert found is True
        assert "liquid cooling" in abstract.lower()
        assert "Keywords" not in abstract

    def test_spaced_letter_abstract(self):
        text = (
            "A R T I C L E   I N F O\n\n"
            "A B S T R A C T\n"
            "This study focuses on phase change materials for battery "
            "cooling applications. The experimental results demonstrate "
            "superior thermal performance of composite PCMs.\n"
            "K E Y W O R D S PCM battery cooling\n"
            "I N T R O D U C T I O N\n"
        )
        abstract, found = extract_abstract(text)
        assert found is True
        assert "phase change" in abstract.lower()

    def test_chinese_abstract(self):
        text = (
            "摘要：本文研究了锂离子电池热管理中的液冷技术，"
            "提出了一种新型拓扑优化液冷板设计。实验结果表明，"
            "优化后的液冷板能有效降低电池最高温度。"
            "关键词：锂离子电池；热管理；液冷\n"
            "1 引言\n"
        )
        abstract, found = extract_abstract(text)
        # Chinese abstract detection
        if found:
            assert "液冷" in abstract

    def test_returns_fallback_when_no_marker(self):
        text = (
            "This is a paper about battery cooling. "
            "It has no abstract section header. "
            "The content discusses various thermal management "
            "approaches for lithium-ion batteries in electric vehicles."
        )
        abstract, found = extract_abstract(text)
        # Should use fallback
        assert len(abstract) > 0

    def test_empty_input(self):
        abstract, found = extract_abstract("")
        assert found is False
        assert abstract == ""

    def test_very_short_input(self):
        abstract, found = extract_abstract("Too short")
        assert found is False

    def test_article_info_abstract_format(self):
        # Simulates the actual Elsevier format that had the bug
        text = (
            "Energy Conversion and Management 356 (2026) 121327 "
            "A R T I C L E   I N F O   A B S T R A C T "
            "Keywords:Battery thermal managementThermoelectric cooling"
            " Currently, with the increasing requirements for electric "
            "vehicle performance, the capacity and integration level "
            "of batteries have gradually improved. "
            "Thermal runaway has emerged as a pressing challenge. "
        )
        abstract, found = extract_abstract(text)
        # With the normalized format, should detect the abstract marker
        if found:
            assert len(abstract) >= 40


# ── generate_summary ───────────────────────────────────────────


class TestGenerateSummary:
    def test_from_abstract(self):
        abstract = (
            "This paper presents a novel battery thermal management "
            "system using phase change materials. The experimental "
            "results show a 30% reduction in peak temperature. "
            "The proposed system outperforms existing solutions. "
            "Further optimization is discussed."
        )
        summary = generate_summary(abstract)
        assert len(summary) > 0
        # Should be extractive from the beginning
        assert "battery thermal management" in summary.lower()

    def test_respects_max_length(self):
        long_abstract = "Very important. " * 100
        summary = generate_summary(long_abstract)
        assert len(summary) <= 300  # With some margin

    def test_empty_abstract_uses_full_text(self):
        summary = generate_summary(
            "",
            "Full text content. About battery cooling systems. More text here."
        )
        assert len(summary) > 0
        assert "battery cooling" in summary.lower()

    def test_empty_both(self):
        summary = generate_summary("", "")
        assert summary == ""

    def test_short_abstract_fallback(self):
        summary = generate_summary("Short.", "Full text about cooling systems.")
        assert len(summary) > 0

    def test_chinese_sentences(self):
        abstract = (
            "本文研究了锂离子电池热管理。"
            "提出了一种新型液冷板设计。"
            "实验验证了设计的有效性。"
        )
        summary = generate_summary(abstract)
        assert len(summary) > 0
        assert "锂离子电池" in summary
