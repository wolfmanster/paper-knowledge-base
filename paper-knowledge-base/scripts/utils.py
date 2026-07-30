"""
论文知识库 — 公共工具函数
==========================
PDF 提取、文本清洗、分块、元数据生成
"""

import hashlib
import logging
import math
import re
import sys
from pathlib import Path
from typing import List, Optional, TYPE_CHECKING

import fitz  # PyMuPDF

if TYPE_CHECKING:
    from sentence_transformers import CrossEncoder, SentenceTransformer

logger = logging.getLogger(__name__)

# ── PDF 文本提取 ─────────────────────────────────────────────

# 跳过超大文件（>25MB — 通常是手册/书籍而非研究论文）
MAX_FILE_SIZE = 25 * 1024 * 1024
# 最多处理前20页（大多数论文的正文在前20页，跳过超大手册）
MAX_PAGES = 20
# 单篇论文最大提取字符数
MAX_TEXT_CHARS = 100_000


def extract_text_from_pdf(pdf_path: Path) -> Optional[str]:
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


# ── 文本清洗 ─────────────────────────────────────────────────

def clean_text(text: str) -> str:
    text = re.sub(r"\n+", "\n", text)
    text = re.sub(r" {2,}", " ", text)
    text = re.sub(r"\[\d+(?:,\s*\d+)*\]", "", text)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"\bdoi:\s*10\.\S+", "", text, flags=re.IGNORECASE)
    return text.strip()


# ── 文本分块 ─────────────────────────────────────────────────

CHUNK_MAX_WORDS = 100


def chunk_text(
    text: str,
    title: str = "",
    max_words: int = CHUNK_MAX_WORDS,
) -> List[dict]:
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


def _detect_section_header(line: str) -> Optional[str]:
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


# ── 元数据 ───────────────────────────────────────────────────

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


# ── 文件类型检测 ─────────────────────────────────────────────

SUPPORTED_EXTENSIONS = {".pdf"}  # .docx 暂未实现提取


def is_supported_file(path: Path) -> bool:
    return path.suffix.lower() in SUPPORTED_EXTENSIONS


# ── 摘要提取 ──────────────────────────────────────────────────

# 期刊页眉常见 boilerplate 模式（在回退提取时需要清理）
_JOURNAL_BOILER = re.compile(
    r"^(?:journal of|international|applied|energy|thermal|"
    r"heat and mass|electrochimica|renewable|sustainable|"
    r"©|copyright|published by|elsevier|springer|mdpi|"
    r"http|doi:|vol\.|pp\.|issn|isbn)",
    re.IGNORECASE,
)


def _strip_journal_header(text: str) -> str:
    """移除期刊页眉 boilerplate 行。"""
    lines = text.split("\n")
    result: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if _JOURNAL_BOILER.match(stripped):
            continue
        result.append(line)
    return "\n".join(result)


def _clean_abstract(text: str) -> str:
    """清理提取后的摘要文本。"""
    # 移除残留的 "Keywords: ..." 后缀
    text = re.sub(r"\b(?:Keywords?|K E Y W O R D S?|Key words?)\b[:\s]*.*$", "", text, flags=re.IGNORECASE | re.DOTALL)
    # 折叠多余空白
    text = re.sub(r"\s+", " ", text)
    # 修复断词连字符
    text = re.sub(r"(\w)-\s(\w)", r"\1\2", text)
    return text.strip()


# 已知的期刊/DOC 中常见的空格字母单词
_SPACED_WORDS = {
    "ABSTRACT", "INTRODUCTION", "KEYWORDS", "ARTICLE", "INFO",
    "RESEARCH", "PAPER", "REVIEW", "METHODOLOGY", "METHODS",
    "RESULTS", "DISCUSSION", "CONCLUSION", "CONCLUSIONS",
    "NOMENCLATURE", "REFERENCES", "BIBLIOGRAPHY", "ACKNOWLEDGMENT",
    "ACKNOWLEDGEMENTS", "APPENDIX", "HIGHLIGHTS", "GRAPHICAL",
    "SUPPLEMENTARY", "MATERIALS", "EXPERIMENTAL", "EXPERIMENT",
    "BACKGROUND", "RELATED", "WORK",
}


def _normalize_spaced_letters(text: str) -> str:
    """将单字母空格单词（A B S T R A C T 等）合并为普通单词。

    识别连续单大写字母+空白序列，对照已知学术词汇表进行拆分和合并。
    例如 "A R T I C L E   I N F O   A B S T R A C T" → " ARTICLE INFO ABSTRACT ".
    """
    # 匹配连续的单大写字母+空白序列
    pattern = re.compile(r'(?:[A-Z](?:\s+|$)){2,}')

    matches = list(pattern.finditer(text))
    if not matches:
        return text

    # 从后往前替换以保持位置正确
    result = text
    for m in reversed(matches):
        raw = m.group(0)
        letters = ''.join(re.findall(r'[A-Z]', raw))
        tokens = _split_letters_by_known_words(letters)
        if tokens:
            replacement = ' ' + ' '.join(tokens) + ' '
            result = result[:m.start()] + replacement + result[m.end():]

    return result


def _split_letters_by_known_words(letters: str) -> list[str]:
    """将连续的字母序列按照已知词汇表拆分。

    从后往前贪心匹配：优先匹配最长的已知单词。
    例如 "ARTICLEINFOABSTRACT" → ["ARTICLE", "INFO", "ABSTRACT"].
    """
    tokens: list[str] = []
    pos = len(letters)
    while pos > 0:
        found = None
        # 优先匹配长单词（从后往前取 4-12 个字符）
        for length in range(min(12, pos), 3, -1):
            candidate = letters[pos - length:pos]
            if candidate in _SPACED_WORDS:
                found = candidate
                break
        if found:
            tokens.append(found)
            pos -= len(found)
        else:
            # 无法匹配，跳过当前位置
            pos -= 1

    tokens.reverse()
    return tokens


def extract_abstract(full_text: str) -> tuple:
    """从论文全文文本中提取摘要。

    返回值: (abstract_text: str, found_marker: bool)
      - found_marker=True 表示检测到了真正的 Abstract 章节标记
      - found_marker=False 表示使用了回退策略
    """
    if not full_text or len(full_text.strip()) < 50:
        return ("", False)

    # 在前 15000 字符中搜索
    head = full_text[:15000]

    # 将空格字母格式标准化
    normalized = _normalize_spaced_letters(head)
    # 折叠多余空白
    normalized = re.sub(r'\s+', ' ', normalized)

    # ── 尝试多个模式，按优先级 ──

    # 1. ABSTRACT ... KEYWORDS / INTRODUCTION（Elsevier 等主流期刊）
    m = re.search(
        r'\bABSTRACT\b\s*(?:\(.*?\))?\s*(.+?)(?:\n\s*|\s{2,})(?:KEYWORDS?|K\s*E\s*Y\s*W\s*O\s*R\s*D)',
        normalized, re.DOTALL | re.IGNORECASE,
    )
    if not m:
        # 宽松模式：Keywords 前即使没有明显分隔也尝试匹配
        m = re.search(
            r'\bABSTRACT\b\s*(?:\(.*?\))?\s*(.+?)\s*KEYWORDS?\b',
            normalized, re.DOTALL | re.IGNORECASE,
        )
    if m:
        abstract = _clean_abstract(m.group(1).strip())
        if len(abstract) >= 40:
            return (abstract, True)

    # 2. ABSTRACT ... (end of pattern) -- 没有 KEYWORDS 终止的情况
    m = re.search(
        r'\bABSTRACT\b\s*(?:\(.*?\))?\s*(.+?)(?:\bINTRODUCTION\b|\d+\.?\s*(?:Introduction|INTR))',
        normalized, re.DOTALL | re.IGNORECASE,
    )
    if m:
        abstract = _clean_abstract(m.group(1).strip())
        if len(abstract) >= 40:
            return (abstract, True)

    # 3. 中文摘要: 摘要 ... 关键词/关键字
    m = re.search(
        r'摘\s*要\s*[:：]?\s*(.+?)(?:关键词|关键字|第[一二三\d]章|引言|绪论|一、)',
        head, re.DOTALL,
    )
    if m:
        abstract = _clean_abstract(m.group(1).strip())
        if len(abstract) >= 20:  # 中文摘要可能较短
            return (abstract, True)

    # 4. Abstract（首字母大写）... Keywords / Introduction
    m = re.search(
        r'\bAbstract\b\s*\n+(.+?)(?:\bKeywords?\b|\bIntroduction\b)',
        normalized, re.DOTALL,
    )
    if m:
        abstract = _clean_abstract(m.group(1).strip())
        if len(abstract) >= 40:
            return (abstract, True)

    # 5. 未规范化的 A B S T R A C T（兜底）
    m = re.search(
        r'A\s*B\s*S\s*T\s*R\s*A\s*C\s*T\s*(?:\(.*?\))?\s*(.+?)\s*(?:K\s*E\s*Y\s*W\s*O\s*R\s*D\s*S?|Keywords?)',
        head, re.DOTALL | re.IGNORECASE,
    )
    if m:
        abstract = _clean_abstract(m.group(1).strip())
        if len(abstract) >= 40:
            return (abstract, True)

    # ── 回退：取全文前 500 字符（跳过期刊页眉） ──
    cleaned = _strip_journal_header(normalized)
    words = cleaned.split()
    if not words:
        return ("", False)
    # 找到第一个大于 1 个字符的单词作为起点
    start = 0
    for i, w in enumerate(words):
        if len(w) > 1 and re.match(r'^[A-Za-z一-鿿]', w):
            start = i
            break
    fallback = ' '.join(words[start:start + 100])
    return (fallback[:500].strip(), False)


def generate_summary(abstract: str, full_text: str = "") -> str:
    """生成提取式摘要。

    策略：取摘要的前 2-3 句（最多 200 字符）。
    如果摘要太短，回退使用全文前 200 字符。
    """
    source = abstract.strip() if abstract and len(abstract.strip()) >= 30 else ""
    if not source and full_text:
        source = _strip_journal_header(full_text[:5000])

    if not source:
        return ""

    # 按中英文句号拆分句子
    sentences = re.split(r"(?<=[.!?。！？])\s+", source)
    summary_parts: list[str] = []
    total = 0
    for sent in sentences:
        sent = sent.strip()
        if not sent:
            continue
        summary_parts.append(sent)
        total += len(sent)
        if total >= 200:
            break

    return " ".join(summary_parts).strip()


# ── 共享常量和函数 ─────────────────────────────────────────
# 所有脚本从 utils.py 导入以下定义，避免重复

SCRIPTS_DIR = Path(__file__).parent
BASE_DIR = SCRIPTS_DIR.parent
CHROMA_DIR = BASE_DIR / "kb" / "chroma"
COLLECTION_NAME = "papers"
BI_ENCODER_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
CROSS_ENCODER_NAME = "cross-encoder/ms-marco-MiniLM-L-6-v2"
INDEX_DB = BASE_DIR / "kb" / "index.db"


def sigmoid(x: float) -> float:
    """将 unbounded logit 映射到 [0, 1] 区间。"""
    try:
        return 1.0 / (1.0 + math.exp(-x))
    except OverflowError:
        return 1.0 if x > 0 else 0.0


def ensure_utf8_stdout() -> None:
    """配置 stdout/stderr 为 UTF-8 编码（Windows GBK 兼容）。"""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, OSError):
                pass


def setup_logging(name: str, log_path: Path, mode: str = "a") -> logging.Logger:
    """配置统一的日志格式，返回名为 name 的 logger。"""
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(fmt)
    logger.addHandler(handler)
    file_handler = logging.FileHandler(str(log_path), mode=mode, encoding="utf-8")
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)
    return logger


def get_version(base_dir: Path) -> str:
    """通过 git describe 获取项目版本，失败时返回 'unknown'。"""
    import subprocess

    try:
        result = subprocess.run(
            ["git", "describe", "--tags", "--always", "--dirty"],
            capture_output=True, text=True,
            cwd=str(base_dir),
            timeout=2,
        )
        return result.stdout.strip() if result.returncode == 0 else "unknown"
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return "unknown"


def has_chinese(text: str) -> bool:
    """检查文本是否包含中文字符（含 CJK 扩展 A 区）。"""
    for ch in text:
        if "一" <= ch <= "鿿" or "㐀" <= ch <= "䶿":
            return True
    return False


def _preferred_model_device() -> str:
    """优先使用可用的 GPU，未检测到 GPU 时回退到 CPU。"""
    import torch

    if torch.cuda.is_available():
        return "cuda"
    mps_backend = getattr(getattr(torch, "backends", None), "mps", None)
    if mps_backend is not None and mps_backend.is_available():
        return "mps"
    return "cpu"


def load_bi_encoder(device: str = "auto") -> "SentenceTransformer":
    """加载 Bi-Encoder，优先使用本地缓存，然后 HuggingFace。"""
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
    if device == "auto":
        device = _preferred_model_device()
    try:
        if model_path.exists():
            return SentenceTransformer(str(model_path), device=device)
        return SentenceTransformer(BI_ENCODER_NAME, device=device)
    except Exception as e:
        logger.error("Bi-Encoder 加载失败: %s", e)
        raise


def load_cross_encoder(device: str = "auto") -> "CrossEncoder | None":
    """加载 Cross-Encoder，失败返回 None（降级为 Bi-Encoder 模式）。"""
    from sentence_transformers import CrossEncoder

    if device == "auto":
        device = _preferred_model_device()
    try:
        return CrossEncoder(CROSS_ENCODER_NAME, device=device)
    except Exception as e:
        logger.warning("Cross-Encoder 加载失败（仅使用 Bi-Encoder）: %s", e)
        return None


def get_or_create_chroma_collection() -> "chromadb.Collection":
    """连接 ChromaDB 并获取或创建论文集合。"""
    import chromadb

    CHROMA_DIR.mkdir(parents=True, exist_ok=True)
    client = chromadb.PersistentClient(str(CHROMA_DIR))
    try:
        return client.get_collection(COLLECTION_NAME)
    except (ValueError, chromadb.errors.NotFoundError):
        return client.create_collection(
            name=COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
        )


def validate_top_k(top_k: int) -> int:
    """验证 top_k 值在有效范围内（1-100），不合法时返回默认值 5。"""
    try:
        k = int(top_k)
    except (ValueError, TypeError):
        return 5
    return max(1, min(k, 100))
