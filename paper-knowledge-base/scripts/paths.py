"""项目路径与共享常量。

集中定义所有路径与跨模块共享的常量，避免循环依赖。
"""

from __future__ import annotations

from pathlib import Path

# ── 目录路径 ─────────────────────────────────────────────────
SCRIPTS_DIR = Path(__file__).parent
BASE_DIR = SCRIPTS_DIR.parent
CHROMA_DIR = BASE_DIR / "kb" / "chroma"
INDEX_DB = BASE_DIR / "kb" / "index.db"

# ── ChromaDB ────────────────────────────────────────────────
COLLECTION_NAME = "papers"

# ── 模型名称 ─────────────────────────────────────────────────
BI_ENCODER_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
CROSS_ENCODER_NAME = "cross-encoder/ms-marco-MiniLM-L-6-v2"

# ── PDF 提取 ─────────────────────────────────────────────────
# 跳过超大文件（>25MB — 通常是手册/书籍而非研究论文）
MAX_FILE_SIZE = 25 * 1024 * 1024
# 最多处理前 20 页（大多数论文的正文在前 20 页，跳过超大手册）
MAX_PAGES = 20
# 单篇论文最大提取字符数
MAX_TEXT_CHARS = 100_000

# ── 文本分块 ─────────────────────────────────────────────────
CHUNK_MAX_WORDS = 100

# ── 文件类型检测 ─────────────────────────────────────────────
SUPPORTED_EXTENSIONS = {".pdf"}  # .docx 暂未实现提取
