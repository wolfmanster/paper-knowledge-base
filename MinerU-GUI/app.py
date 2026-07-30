"""
MinerU 文档解析工具 - 核心逻辑
tkinter GUI 通过调用此模块的函数执行转换。
"""

from __future__ import annotations

import importlib.metadata
import re
from pathlib import Path


# ── Version ──────────────────────────────────────────────────
def _get_version() -> str:
    """Return the package version — from importlib.metadata (installed) or fallback."""
    try:
        return importlib.metadata.version("mineru-gui")
    except importlib.metadata.PackageNotFoundError:
        return "0.0.0.dev0"

__version__ = _get_version()
"""Package version (str) — readable after ``from app import __version__``."""

# ── 配置 ──────────────────────────────────────────────────
PROJECT_DIR = Path(__file__).parent.resolve()
OUTPUT_DIR = PROJECT_DIR / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

LANGUAGES = [
    "ch", "ch_server", "ch_lite", "en", "korean", "japan",
    "chinese_cht", "ta", "te", "ka", "th", "el", "latin",
    "arabic", "east_slavic", "cyrillic", "devanagari",
]

BACKENDS = ["pipeline", "hybrid-auto-engine"]

SUPPORTED_EXTENSIONS = (
    ".pdf", ".png", ".jpg", ".jpeg", ".jp2", ".webp",
    ".gif", ".bmp", ".tiff", ".docx",
)


# ── 辅助函数 ──────────────────────────────────────────────

def _find_output_md(output_dir: Path, file_stem: str) -> Path | None:
    """在输出目录中递归查找生成的 .md 文件。"""
    if not output_dir.exists():
        return None
    for md_file in output_dir.rglob("*.md"):
        if md_file.stem == file_stem:
            return md_file
    return None


def _clean_orphaned_images(content: str, images_dir: Path) -> int:
    """删除 images_dir 中未被 MD 内容引用的孤立图片文件。返回删除数。"""
    if not images_dir.exists():
        return 0

    referenced: set[str] = set()
    for m in re.finditer(r'!\[([^\]]*)\]\(([^)]+)\)', content):
        referenced.add(Path(m.group(2)).name)

    deleted = 0
    for f in list(images_dir.iterdir()):
        if f.is_file() and f.name not in referenced:
            f.unlink()
            deleted += 1
    return deleted


# ── DPI 缩放 ──────────────────────────────────────────────


def get_dpi_scale(root) -> float:
    """获取当前 DPI 缩放因子。1.0 = 100%，1.25 = 125%，2.0 = 200% 等。

    必须在已有 Tk root 实例后调用。
    """
    try:
        dpi = root.winfo_fpixels("1i")
        return max(1.0, round(dpi / 96, 2))
    except (AttributeError, OSError, RuntimeError):
        return 1.0
