"""
MinerU document conversion API — callable from other Python projects.

Usage::

    import sys
    sys.path.insert(0, r"path/to/MinerU GUI")
    from mineru_api import convert_document, batch_convert

    # Single file
    result = convert_document("report.pdf")
    if result.success:
        print(f"Output: {result.output_md}")

    # Batch
    results = batch_convert(["a.pdf", "b.pdf", "c.png"])
    for r in results:
        print(f"{'OK' if r.success else 'FAIL'} {r.file_path}")

    # Custom output directory
    result = convert_document("doc.pdf", output_dir="./my_output/doc")

.. note::

    Environment variables (MINERU_DEVICE_MODE, OMP_NUM_THREADS, etc.) are
    process-global state.  ``convert_document`` and ``batch_convert`` are
    **not thread-safe** — do not call them from multiple threads concurrently.
    For concurrency, use ``multiprocessing`` and call these functions in
    child processes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from gui._core import run_core

# ── Version ─────────────────────────────────────────────

def _get_version() -> str:
    """Return the package version — from importlib.metadata (installed) or fallback."""
    try:
        import importlib.metadata
        return importlib.metadata.version("mineru-gui")
    except Exception:  # noqa: BLE001 — broad catch for PackageNotFoundError
        try:
            from _version import __version__  # type: ignore[import-untyped]
            return __version__
        except ImportError:
            return "0.0.0"

__version__ = _get_version()
"""Package version (str) — readable after ``from mineru_api import __version__``."""


@dataclass
class ConversionResult:
    """Result of a single document conversion."""

    success: bool = False
    """Whether the conversion succeeded."""
    file_path: str = ""
    """Input file path."""
    output_md: Path | None = None
    """Path to the generated Markdown file (on success)."""
    output_dir: Path | None = None
    """Output directory (on success)."""
    log_lines: list[str] = field(default_factory=list)
    """Log lines produced during processing."""
    error: str | None = None
    """Error message (on failure)."""


# ── Public API ─────────────────────────────────────────


def convert_document(
    file_path: str | Path,
    backend: str = "hybrid-auto-engine",
    lang: str = "ch",
    method: str = "auto",
    max_pages: int = 0,
    device: str = "gpu",
    vlm_describe: bool = False,
    output_dir: str | Path | None = None,
) -> ConversionResult:
    """Convert a single document.

    Args:
        file_path: Path to the PDF / image / Office file.
        backend: ``"pipeline"`` or ``"hybrid-auto-engine"``.
        lang: OCR language code (see ``app.LANGUAGES``).
        method: ``"auto"`` / ``"txt"`` / ``"ocr"``.
        max_pages: Max pages to process (``0`` = all).
        device: ``"cpu"`` or ``"gpu"``.  Hybrid backend always uses GPU.
        vlm_describe: Whether to describe images via VLM (hybrid only).
        output_dir: Custom output directory.  ``None`` uses
            ``./output/<filename>/``.

    Returns:
        :class:`ConversionResult`.
    """
    logs: list[str] = []

    def _log(line: str) -> None:
        logs.append(line)

    success, output_md, out_dir, error = run_core(
        file_path=str(file_path),
        backend=backend,
        lang=lang,
        method=method,
        max_pages=max_pages,
        device=device,
        vlm_describe=vlm_describe,
        output_dir_str=str(output_dir) if output_dir is not None else None,
        log=_log,
    )

    return ConversionResult(
        success=success,
        file_path=str(file_path),
        output_md=output_md,
        output_dir=out_dir,
        log_lines=logs,
        error=error,
    )


def batch_convert(
    file_paths: list[str | Path],
    backend: str = "hybrid-auto-engine",
    lang: str = "ch",
    method: str = "auto",
    max_pages: int = 0,
    device: str = "gpu",
    vlm_describe: bool = False,
    output_dir: str | Path | None = None,
) -> list[ConversionResult]:
    """Batch-convert multiple documents.

    Accepts the same parameters as :func:`convert_document`.
    Returns results in the same order as the input list.
    """
    results: list[ConversionResult] = []
    total = len(file_paths)

    for i, fp in enumerate(file_paths):
        logs: list[str] = []

        def _log(line: str, _logs=logs) -> None:
            _logs.append(line)

        success, output_md, out_dir, error = run_core(
            file_path=str(fp),
            backend=backend,
            lang=lang,
            method=method,
            max_pages=max_pages,
            device=device,
            vlm_describe=vlm_describe,
            output_dir_str=str(output_dir) if output_dir is not None else None,
            log=_log,
            batch_index=(i + 1, total),
        )

        results.append(
            ConversionResult(
                success=success,
                file_path=str(fp),
                output_md=output_md,
                output_dir=out_dir,
                log_lines=logs,
                error=error,
            )
        )

    return results
