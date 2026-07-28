"""
Background worker for MinerU document conversion.
Runs :func:`gui._core.run_core` in a daemon thread, communicates via
``queue.Queue`` + ``threading.Event``.
Supports single and batch file processing.
"""

from __future__ import annotations

import queue
import threading
from pathlib import Path

from gui._core import run_core

_log_queue: queue.Queue | None = None
_done_event: threading.Event | None = None


def init_worker(log_q: queue.Queue, done_evt: threading.Event) -> None:
    """Initialise module-level queue and done-event."""
    global _log_queue, _done_event
    _log_queue = log_q
    _done_event = done_evt


def start_batch_conversion(
    file_paths: list[str],
    backend: str,
    lang: str,
    method: str,
    max_pages: int,
    device: str,
    vlm_describe: bool = False,
) -> None:
    """Start a background thread that converts all given files."""
    t = threading.Thread(
        target=_run_batch_conversion,
        args=(file_paths, backend, lang, method, max_pages, device, vlm_describe),
        daemon=True,
    )
    t.start()


# ── Batch conversion ────────────────────────────────────


def _run_batch_conversion(
    file_paths: list[str],
    backend: str,
    lang: str,
    method: str,
    max_pages: int,
    device: str,
    vlm_describe: bool = False,
) -> None:
    total = len(file_paths)
    success_count = 0
    fail_count = 0

    _enqueue(f"📦 批量转换: 共 {total} 个文件\n\n")

    for i, file_path in enumerate(file_paths, 1):
        input_path = Path(file_path)
        _enqueue(f"\n{'=' * 50}\n")
        _enqueue(f"[{i}/{total}] {input_path.name}\n")
        _enqueue(f"{'=' * 50}\n")

        ok, _, _, _ = run_core(
            file_path=file_path,
            backend=backend,
            lang=lang,
            method=method,
            max_pages=max_pages,
            device=device,
            vlm_describe=vlm_describe,
            output_dir_str=None,
            log=_enqueue,
            batch_index=(i, total),
        )
        if ok:
            success_count += 1
        else:
            fail_count += 1

    _enqueue(f"\n{'=' * 50}\n")
    _enqueue("📊 批量转换完成\n")
    _enqueue(f"   ✓ 成功: {success_count}\n")
    if fail_count:
        _enqueue(f"   ✗ 失败: {fail_count}\n")
    _enqueue(f"{'=' * 50}\n")

    if _done_event is not None:
        _done_event.set()


# ── Internal helpers ────────────────────────────────────


def _enqueue(line: str) -> None:
    if _log_queue is not None:
        _log_queue.put(line + "\n")
