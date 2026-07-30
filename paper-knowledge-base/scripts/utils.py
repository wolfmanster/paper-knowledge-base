"""论文知识库 — 运行时工具函数

仅包含跨模块共享的运行时工具：
  - sigmoid：logit → [0, 1] 归一化
  - ensure_utf8_stdout：Windows GBK 终端 UTF-8 兼容
  - setup_logging：统一日志格式
  - get_version：通过 git describe 获取版本
  - has_chinese：CJK 字符检测

PDF 提取/分块/摘要等工具已拆分到独立模块：
  - paths：路径与共享常量
  - text_processing：PDF/Markdown 提取、清洗、分块、元数据
  - abstract_extractor：摘要提取与摘要摘要生成
  - models：Bi/Cross-Encoder 加载、ChromaDB 集合管理
"""

from __future__ import annotations

import logging
import math
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


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
