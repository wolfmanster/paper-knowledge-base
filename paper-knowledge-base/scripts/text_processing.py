"""PDF / Markdown 文本提取、清洗、分块与元数据工具。

包含:
  - PDF 全文提取（PyMuPDF）
  - 文本清洗（去引用/URL/DOI）
  - 段落级分块（章节检测 + 词数切分）
  - Markdown → 纯文本转换
  - 元数据辅助函数（paper_id / 年份 / 标题解析）
  - 文件类型检测
"""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path

import fitz  # PyMuPDF

from paths import (
    CHUNK_MAX_WORDS,
    MAX_FILE_SIZE,
    MAX_PAGES,
    MAX_TEXT_CHARS,
    SUPPORTED_EXTENSIONS,
)

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
#  PDF 文本提取
# ═══════════════════════════════════════════════════════════════

def extract_text_from_pdf(pdf_path: Path) -> str | None:
    """用 PyMuPDF 提取 PDF 全文，返回纯文本，失败返回 None。

    - 跳过 >25MB 的超大文件（手册/书籍）
    - 只处理前 MAX_PAGES 页
    - 限制最大文本长度
    """
    try:
        # 跳过超大文件（stat() 在 try 块内，防止文件被删除/权限错误）
        if pdf_path.stat().st_size > MAX_FILE_SIZE:
            return None
        doc = fitz.open(pdf_path)
    except Exception:
        return None

    total_pages = len(doc)
    all_text_parts: list[str] = []
    ref_reached = False
    ref_pattern = re.compile(
        r"^(references|bibliography|参考文献|文献)", re.IGNORECASE
    )
    total_len = 0

    for page_num in range(min(total_pages, MAX_PAGES)):
        if total_len > MAX_TEXT_CHARS:
            break

        page = doc[page_num]
        page_height = page.rect.height

        try:
            blocks = page.get_text("dict")["blocks"]
        except Exception:
            continue

        text_blocks: list[tuple[str, float, float]] = []
        for block in blocks:
            if block["type"] != 0:
                continue
            x0, y0, x1, y1 = block["bbox"]
            if y1 < page_height * 0.05 or y0 > page_height * 0.95:
                continue
            text = _block_to_text(block)
            if not text.strip():
                continue
            text_blocks.append((text, x0, y0))

        text_blocks.sort(key=lambda b: (round(b[2], -1), b[1]))

        for text, _, _ in text_blocks:
            line = text.strip()
            if ref_reached:
                continue
            if ref_pattern.match(line):
                ref_reached = True
                continue
            all_text_parts.append(line)
            total_len += len(line)

    doc.close()
    return "\n".join(all_text_parts) if all_text_parts else None


def _block_to_text(block: dict) -> str:
    spans = []
    for line in block.get("lines", []):
        for span in line.get("spans", []):
            spans.append(span.get("text", ""))
    raw = "".join(spans)
    raw = re.sub(r"(\w)-\n(\w)", r"\1\2", raw)
    return raw


# ═══════════════════════════════════════════════════════════════
#  文本清洗
# ═══════════════════════════════════════════════════════════════

def clean_text(text: str) -> str:
    text = re.sub(r"\n+", "\n", text)
    text = re.sub(r" {2,}", " ", text)
    text = re.sub(r"\[\d+(?:,\s*\d+)*\]", "", text)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"\bdoi:\s*10\.\S+", "", text, flags=re.IGNORECASE)
    return text.strip()


# ═══════════════════════════════════════════════════════════════
#  文本分块
# ═══════════════════════════════════════════════════════════════

def chunk_text(
    text: str,
    title: str = "",
    max_words: int = CHUNK_MAX_WORDS,
) -> list[dict]:
    """将文本切分为适合嵌入的块。

    策略：段落级分割 → 词数分块（简单可靠，避免正则陷阱）。
    """
    text = clean_text(text)
    if len(text) < 20:
        return []

    try:
        raw_paragraphs = re.split(r"\n\s*\n", text)

        chunks: list[dict] = []
        current_section = "Abstract"

        for para in raw_paragraphs:
            para = para.strip()
            if not para:
                continue

            first_line = para.split("\n")[0].strip()
            detected = _detect_section_header(first_line)
            if detected:
                current_section = detected

            words = para.split()
            if not words:
                continue

            if len(words) <= max_words:
                chunks.append({"text": " ".join(words), "section": current_section})
            else:
                for i in range(0, len(words), max_words):
                    segment = " ".join(words[i : i + max_words])
                    if segment.strip():
                        chunks.append({"text": segment, "section": current_section})

        for idx, c in enumerate(chunks):
            c["chunk_index"] = idx

        return chunks
    except Exception as e:
        logger.warning("chunk_text 出错: %s，返回全文", e)
        words = text.split()
        return [
            {"text": " ".join(words[:max_words]), "section": "Full Text", "chunk_index": 0}
        ]


# 已知章节标题（小集合，快速匹配）
_SECTION_NAMES = {
    "abstract", "introduction", "background", "related work", "method",
    "methodology", "experiment", "experimental", "results", "result",
    "discussion", "conclusion", "conclusions", "nomenclature",
    "acknowledgment", "acknowledgements", "references", "bibliography",
    "摘要", "引言", "方法", "实验", "结果", "讨论", "结论", "参考文献",
    "附录",
}

# 编号标题：如 "2.", "2.1", "2.1.1", "III.", "A."
_NUM_PREFIX = re.compile(r"^(\d+(?:\.\d+)*|[IVXLCDM]+|[A-Z])[\.\s]+(.+)$")


def _detect_section_header(line: str) -> str | None:
    """检测一行是否是章节标题。是则返回规范化章节名，否则返回 None。"""
    stripped = line.lower().strip().rstrip(".: ")
    if stripped in _SECTION_NAMES:
        return line.strip()[:60]

    m = _NUM_PREFIX.match(stripped)
    if m:
        title_part = m.group(2).strip().rstrip(".: ")
        if title_part and len(title_part.split()) <= 8:
            if re.match(r"^[a-z一-鿿]", title_part):
                return line.strip()[:60]

    return None


# ═══════════════════════════════════════════════════════════════
#  元数据
# ═══════════════════════════════════════════════════════════════

def compute_paper_id(filepath: Path) -> str:
    raw = str(filepath.resolve()).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:12]


def compute_paper_id_from_doi(doi: str) -> str:
    """从 DOI 计算 paper_id（归一化后取 SHA256 前 12 位）。

    与 compute_paper_id() 一样是 12 位 hex，但基于 DOI 而不是文件路径。
    这样同一篇论文无论如何来源都能产生相同的 paper_id。
    """
    normalized = doi.strip().lower().rstrip(".")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]


def extract_year_from_date(date_str: str) -> str:
    """从 Zotero 日期字符串中提取年份。

    处理格式: "2024-03-15", "2024/Mar", "2024", "2024-00-00" 等。
    提取失败返回空字符串。
    """
    if not date_str:
        return ""
    m = re.search(r"(\d{4})", str(date_str))
    return m.group(1) if m else ""


def extract_text_from_markdown(md_text: str) -> str:
    """将 Markdown 文本转换为纯文本，保留段落结构。

    移除:
      - 图片引用 ![alt](path)
      - 链接 [text](url) → text
      - # 标题标记
      - 表格格式（保留单元格文本）
      - 代码块标记
      - 水平分割线
    """
    if not md_text:
        return ""

    text = md_text

    # 移除代码块
    text = re.sub(r"```[\s\S]*?```", "", text)
    text = re.sub(r"~~~[\s\S]*?~~~", "", text)

    # 图片引用 → 空
    text = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", text)

    # 链接 [text](url) → text
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)

    # 移除标题标记 (### 等)，保留标题文字
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)

    # 移除粗体/斜体标记
    text = re.sub(r"\*{1,3}([^*]+)\*{1,3}", r"\1", text)
    text = re.sub(r"_{1,3}([^_]+)_{1,3}", r"\1", text)

    # 表格：移除 | 和 --- 分隔线，保留单元格内容
    text = re.sub(r"^\|(.+)\|$", r"\1", text, flags=re.MULTILINE)
    text = re.sub(r"^[-:| ]+$", "", text, flags=re.MULTILINE)
    text = re.sub(r"\|", " ", text)

    # 水平分割线
    text = re.sub(r"^[-*_]{3,}\s*$", "", text, flags=re.MULTILINE)

    # 移除 html 标签
    text = re.sub(r"<[^>]+>", "", text)

    # 折叠空行
    text = re.sub(r"\n{3,}", "\n\n", text)

    # 去除首尾空白
    text = text.strip()

    return text


def extract_title_from_filename(filepath: Path) -> str:
    name = filepath.stem
    name = re.sub(r"\(科研通-ablesci\.com\).*", "", name)
    name = re.sub(r"\(科研通-ablesci\.com\)\s*", "", name)
    name = re.sub(r"_\d+$", "", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name


# ═══════════════════════════════════════════════════════════════
#  文件类型检测
# ═══════════════════════════════════════════════════════════════

def is_supported_file(path: Path) -> bool:
    return path.suffix.lower() in SUPPORTED_EXTENSIONS
