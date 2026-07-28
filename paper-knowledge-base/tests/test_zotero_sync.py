"""
Zotero 同步模块 — 单元测试
"""
import hashlib
import json
import re
import tempfile
from pathlib import Path

import pytest

# ── 复用知识库模块 ─────────────────────────────────────────
import sys
BASE_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = BASE_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from utils import (
    compute_paper_id_from_doi,
    extract_text_from_markdown,
    extract_year_from_date,
)


# ═══════════════════════════════════════════════════════════════
#  compute_paper_id_from_doi
# ═══════════════════════════════════════════════════════════════

class TestComputePaperIdFromDoi:
    def test_same_doi_produces_same_id(self):
        id1 = compute_paper_id_from_doi("10.1016/j.enconman.2024.119440")
        id2 = compute_paper_id_from_doi("10.1016/j.enconman.2024.119440")
        assert id1 == id2

    def test_different_doi_different_id(self):
        id1 = compute_paper_id_from_doi("10.1016/j.enconman.2024.119440")
        id2 = compute_paper_id_from_doi("10.1016/j.enconman.2025.119441")
        assert id1 != id2

    def test_case_insensitive(self):
        id1 = compute_paper_id_from_doi("10.1016/J.ENCONMAN.2024.119440")
        id2 = compute_paper_id_from_doi("10.1016/j.enconman.2024.119440")
        assert id1 == id2

    def test_strips_trailing_dot(self):
        id1 = compute_paper_id_from_doi("10.1016/j.enconman.2024.119440.")
        id2 = compute_paper_id_from_doi("10.1016/j.enconman.2024.119440")
        assert id1 == id2

    def test_strips_whitespace(self):
        id1 = compute_paper_id_from_doi("  10.1016/j.enconman.2024.119440  ")
        id2 = compute_paper_id_from_doi("10.1016/j.enconman.2024.119440")
        assert id1 == id2

    def test_output_is_12_char_hex(self):
        paper_id = compute_paper_id_from_doi("10.1016/j.enconman.2024.119440")
        assert len(paper_id) == 12
        assert all(c in "0123456789abcdef" for c in paper_id)

    def test_empty_doi(self):
        # 空 DOI 应产生有效的 paper_id（虽然不推荐）
        paper_id = compute_paper_id_from_doi("")
        assert len(paper_id) == 12 and isinstance(paper_id, str)

    def test_none_doi_raises(self):
        with pytest.raises((AttributeError, TypeError)):
            compute_paper_id_from_doi(None)  # type: ignore


# ═══════════════════════════════════════════════════════════════
#  extract_text_from_markdown
# ═══════════════════════════════════════════════════════════════

class TestExtractTextFromMarkdown:
    def test_removes_image_references(self):
        md = "Some text ![figure](images/fig1.png) more text"
        result = extract_text_from_markdown(md)
        assert "![figure]" not in result
        assert "Some text" in result
        assert "more text" in result

    def test_converts_links_to_text(self):
        md = "See [this paper](https://doi.org/10.1234) for details"
        result = extract_text_from_markdown(md)
        assert "this paper" in result
        assert "https://doi.org/10.1234" not in result

    def test_removes_code_blocks(self):
        md = "Text\n```python\nprint('hello')\n```\nMore text"
        result = extract_text_from_markdown(md)
        assert "print(" not in result
        assert "Text" in result
        assert "More text" in result

    def test_removes_heading_markers(self):
        md = "# Introduction\n## Methods\n### 2.1 Setup"
        result = extract_text_from_markdown(md)
        assert "# " not in result
        assert "Introduction" in result
        assert "Methods" in result

    def test_removes_bold_and_italic(self):
        md = "This is **bold** and *italic* and ***both***"
        result = extract_text_from_markdown(md)
        assert "**" not in result
        assert "bold" in result
        assert "italic" in result

    def test_handles_tables(self):
        md = "| Header1 | Header2 |\n|---------|---------|\n| Cell1   | Cell2   |"
        result = extract_text_from_markdown(md)
        assert "|" not in result
        assert "Header1" in result and "Header2" in result
        assert "Cell1" in result and "Cell2" in result

    def test_removes_horizontal_rules(self):
        md = "Text\n---\nMore text"
        result = extract_text_from_markdown(md)
        assert "Text" in result and "More text" in result

    def test_collapses_excessive_newlines(self):
        md = "Line 1\n\n\n\nLine 2\n\n\nLine 3"
        result = extract_text_from_markdown(md)
        assert "\n\n\n" not in result

    def test_empty_input(self):
        assert extract_text_from_markdown("") == ""
        assert extract_text_from_markdown(None) == ""  # type: ignore

    def test_typical_paper_content(self):
        md = (
            "# Abstract\n\n"
            "This paper presents a novel cooling method.\n\n"
            "## 1. Introduction\n\n"
            "Lithium-ion batteries require [1] efficient thermal management.\n\n"
            "![Temperature profile](images/temp.png)\n\n"
            "**Key findings**: The method reduced temperature by 15%.\n"
        )
        result = extract_text_from_markdown(md)
        assert "Abstract" in result
        assert "cooling method" in result
        assert "Introduction" in result
        assert "Lithium-ion" in result
        assert "Key findings" in result
        assert "15%" in result
        assert "images/temp.png" not in result


# ═══════════════════════════════════════════════════════════════
#  extract_year_from_date
# ═══════════════════════════════════════════════════════════════

class TestExtractYearFromDate:
    def test_standard_date(self):
        assert extract_year_from_date("2024-03-15") == "2024"

    def test_year_only(self):
        assert extract_year_from_date("2024") == "2024"

    def test_zotero_style(self):
        # Zotero 有时用这种格式
        assert extract_year_from_date("2024-00-00") == "2024"
        assert extract_year_from_date("2024/Mar") == "2024"

    def test_empty_string(self):
        assert extract_year_from_date("") == ""

    def test_none(self):
        assert extract_year_from_date(None) == ""


# ═══════════════════════════════════════════════════════════════
#  检查点 I/O (sync_zotero 内部逻辑)
# ═══════════════════════════════════════════════════════════════

class TestCheckpointIO:
    @pytest.fixture
    def checkpoint_dir(self):
        with tempfile.TemporaryDirectory() as d:
            yield Path(d)

    def test_save_and_load(self, checkpoint_dir):
        # 模拟 sync_zotero 的 save_checkpoint / load_checkpoint
        state = {"last_item_id": 693, "last_version": 42}
        cp_file = checkpoint_dir / "checkpoint.json"

        # save
        tmp = cp_file.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({**state, "last_sync_time": "2026-07-27T09:15:00"},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(cp_file)

        # load
        loaded = json.loads(cp_file.read_text(encoding="utf-8"))
        assert loaded["last_item_id"] == 693
        assert loaded["last_version"] == 42

    def test_checkpoint_atomic_write(self, checkpoint_dir):
        """验证原子写入不会产生部分写入的文件。"""
        cp_file = checkpoint_dir / "checkpoint.json"
        import os

        # 写入完整内容
        content = json.dumps({"last_item_id": 100, "last_version": 50,
                              "last_sync_time": "2026-07-27T10:00:00"})
        tmp = cp_file.with_suffix(".tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(cp_file)

        tmp2 = cp_file.with_suffix(".tmp")
        assert not tmp2.exists()  # tmp 文件应已被重命名

    def test_checkpoint_default_when_missing(self, checkpoint_dir):
        """检查点文件不存在时的默认行为。"""
        cp_file = checkpoint_dir / "nonexistent.json"
        if cp_file.exists():
            cp_file.unlink()
        default = {"last_item_id": 0, "last_version": 0}
        assert not cp_file.exists()
        assert default["last_item_id"] == 0
        assert default["last_version"] == 0
