"""MinerU subprocess 提取与进程管理。

从 sync_zotero.py 拆分而来。负责定位 MinerU venv、解析附件路径、
带超时的进程树终止、以及调用 mineru_extract.py 提取文档文本。

模块全局 MINERU_DIR / MINERU_PYTHON 由调用方（sync_zotero._main_unlocked）
在解析 CLI / 环境变量后赋值；extract_with_mineru 读取这两个全局。
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

# ── MinerU 路径全局（由 sync_zotero._main_unlocked 在运行时赋值）────
# MinerU GUI 目录：通过环境变量 MINERU_DIR 或 CLI 参数 --mineru-dir 指定
MINERU_DIR: Path | None = None
MINERU_PYTHON: Path | None = None
MINERU_SCRIPT = Path(__file__).resolve().parent / "mineru_extract.py"
DEFAULT_MINERU_TIMEOUT_SECONDS = 24 * 60 * 60


def resolve_mineru_python(mineru_dir: Path) -> Path:
    """Locate the MinerU virtualenv interpreter on Windows or POSIX systems."""
    candidates = (
        mineru_dir / ".venv" / "Scripts" / "python.exe",
        mineru_dir / ".venv" / "bin" / "python",
        mineru_dir / ".venv" / "bin" / "python3",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0] if os.name == "nt" else candidates[1]


def resolve_attachment_path(
    zotero_storage: Path,
    attachment_key: str,
    content_type: str,
    max_size_mb: int = 500,
) -> Path | None:
    """在 Zotero storage/ 目录中查找附件文件。

    根据 content_type 查找对应后缀的文件（.pdf 或 .docx）。
    跳过超过 max_size_mb 的超大文件。

    Args:
        zotero_storage: 指向 Zotero 的 storage/ 目录
        attachment_key: 附件项的 items.key
        content_type: MIME 类型（决定要查找的文件后缀）
        max_size_mb: 文件大小上限（MB），超过返回 None

    Returns:
        第一个找到的匹配文件路径，未找到返回 None
    """
    storage_dir = zotero_storage / attachment_key
    if not storage_dir.is_dir():
        logger.debug("  storage 目录不存在: %s", storage_dir)
        return None

    if content_type == "application/pdf":
        ext_pattern = "*.pdf"
        type_label = "PDF"
    elif content_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
        ext_pattern = "*.docx"
        type_label = "Word"
    else:
        logger.debug("  不支持的附件类型: %s", content_type)
        return None

    files = sorted(storage_dir.glob(ext_pattern))
    if not files:
        logger.debug("  目录中无 %s 文件: %s", type_label, storage_dir)
        return None

    file_path = files[0]
    if len(files) > 1:
        logger.debug("  storage 目录中有多个 %s，取第一个: %s", type_label, file_path.name)

    # 超大文件检查
    max_bytes = max_size_mb * 1024 * 1024
    try:
        if file_path.stat().st_size > max_bytes:
            logger.warning("  %s 文件过大 (>%dMB)，跳过: %s (%.1fMB)",
                           type_label, max_size_mb, file_path.name, file_path.stat().st_size / (1024*1024))
            return None
    except OSError as e:
        logger.warning("  无法读取文件大小: %s — %s", file_path.name, e)
        return None

    return file_path


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    """Forcefully terminate a MinerU process and all descendants."""
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            check=False,
        )
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        process.kill()


def run_captured_process(
    command: list[str],
    *,
    timeout_seconds: float,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a captured subprocess with a timeout that also kills descendants."""
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be greater than zero")

    process_kwargs: dict = {}
    if os.name == "nt":
        process_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        process_kwargs["start_new_session"] = True

    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        **process_kwargs,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        _terminate_process_tree(process)
        process.communicate()
        raise
    return subprocess.CompletedProcess(
        command,
        process.returncode,
        stdout=stdout,
        stderr=stderr,
    )


def extract_with_mineru(
    file_path: Path,
    output_dir: Path,
    *,
    lang: str = "en",
    max_pages: int = 0,
    timeout_seconds: float = DEFAULT_MINERU_TIMEOUT_SECONDS,
) -> dict | None:
    """通过 MinerU subprocess 提取文档文本。

    支持 PDF 和 Word (.docx) 文件，调用 mineru_extract.py（在 MinerU venv 中运行），
    读取 stdout 中的 JSON 结果。

    Args:
        file_path: 文件路径（.pdf 或 .docx）
        output_dir: MinerU 输出目录
        lang: OCR 语言（默认 en）
        max_pages: 最多处理页数；0 表示全部页面（Word 文件忽略此参数）
        timeout_seconds: 单篇 MinerU 最长运行秒数（默认 24 小时）

    Returns:
        {"text": "pure text", "markdown": "raw md"}，失败返回 None
    """
    if MINERU_PYTHON is None:
        logger.error("  MinerU 路径未配置，跳过文档提取")
        logger.error("  请通过 --mineru-dir 参数或 MINERU_DIR 环境变量指定 MinerU GUI 目录")
        return None

    if not MINERU_PYTHON.exists():
        logger.error("  MinerU Python 不存在: %s", MINERU_PYTHON)
        logger.error("  请确认 MinerU GUI 路径")
        return None

    if not MINERU_SCRIPT.exists():
        logger.error("  mineru_extract.py 不存在: %s", MINERU_SCRIPT)
        return None

    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        # 设置 MINERU_GUI_DIR 环境变量，让 mineru_extract.py 能找到 mineru_api
        env = os.environ.copy()
        env["MINERU_GUI_DIR"] = str(MINERU_DIR)

        proc = run_captured_process(
            [
                str(MINERU_PYTHON),
                str(MINERU_SCRIPT),
                str(file_path),
                str(output_dir),
                "--lang", lang,
                "--max_pages", str(max_pages),
            ],
            timeout_seconds=timeout_seconds,
            env=env,
        )

        # 解析输出 JSON
        if proc.returncode != 0:
            logger.warning("  MinerU 进程返回错误码 %d", proc.returncode)
            logger.debug("  stderr: %s", proc.stderr[:500])
            return None

        result = json.loads(proc.stdout.strip())
        if result.get("status") == "ok":
            return result
        else:
            logger.warning("  MinerU 提取失败: %s", result.get("message", "未知错误"))
            return None

    except subprocess.TimeoutExpired:
        logger.warning(
            "  MinerU 超过 %.1f 小时，已终止进程树并保留到重试队列: %s",
            timeout_seconds / 3600,
            file_path.name,
        )
        return None
    except json.JSONDecodeError as e:
        logger.warning("  MinerU 输出解析失败: %s", e)
        logger.debug("  原始输出: %s", proc.stdout[:300] if proc else "N/A")
        return None
    except Exception as e:
        logger.warning("  MinerU 调用失败: %s", e)
        return None
