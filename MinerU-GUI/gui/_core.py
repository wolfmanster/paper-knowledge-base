"""
Shared core conversion logic.

Used by both ``gui/worker.py`` (GUI daemon thread) and ``mineru_api.py``
(standalone Python API) to avoid code duplication.

The single entry point :func:`run_core` accepts a *log callback* so each
consumer can route log output to its own destination (``queue.Queue``,
``list[str]``, etc.).
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import traceback
from collections.abc import Callable
from pathlib import Path

from loguru import logger as _loguru

from app import OUTPUT_DIR
from app import _clean_orphaned_images as _clean_images
from app import _find_output_md as _find_md

_logger = logging.getLogger(__name__)

# Regex for MinerU page progress ("page 3 / 10")
_PROGRESS_REGEX = re.compile(r"page\s*(\d+)\s*/\s*(\d+)", re.IGNORECASE)

# A log callback accepts a single string line.
LogCallback = Callable[[str], None]


def parse_log_for_progress(msg: str) -> tuple[int, int] | None:
    """Try to extract ``(current_page, total_pages)`` from a log line."""
    m = _PROGRESS_REGEX.search(msg)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None


def setup_env(backend: str, device: str) -> None:
    """Set MinerU environment variables according to backend and device."""
    if backend == "hybrid-auto-engine" or device == "gpu":
        os.environ["MINERU_DEVICE_MODE"] = "cuda"
        os.environ["MINERU_PDF_RENDER_THREADS"] = "1"
    else:
        n_cpus = os.cpu_count() or 4
        os.environ["MINERU_DEVICE_MODE"] = "cpu"
        os.environ["MINERU_PDF_RENDER_THREADS"] = str(n_cpus)
        os.environ["MINERU_VIRTUAL_VRAM_SIZE"] = "32"
        os.environ["OMP_NUM_THREADS"] = str(n_cpus)


# ── Core conversion ────────────────────────────────────


def run_core(
    file_path: str,
    backend: str,
    lang: str,
    method: str,
    max_pages: int,
    device: str,
    vlm_describe: bool,
    output_dir_str: str | None,
    log: LogCallback,
    batch_index: tuple[int, int] | None = None,
) -> tuple[bool, Path | None, Path | None, str | None]:
    """Execute a single document conversion.

    This is the shared synchronous worker that both the GUI thread and the
    standalone API delegate to.

    Parameters
    ----------
    file_path:
        Path to the input file (PDF, image, or Office document).
    backend:
        ``"pipeline"`` or ``"hybrid-auto-engine"``.
    lang:
        OCR language code (see ``app.LANGUAGES``).
    method:
        ``"auto"`` / ``"txt"`` / ``"ocr"``.
    max_pages:
        Maximum pages to process; ``0`` means all.
    device:
        ``"cpu"`` or ``"gpu"``.  Hybrid backend always uses GPU.
    vlm_describe:
        Whether to post-process the markdown with VLM image descriptions
        (``hybrid-auto-engine`` only).
    output_dir_str:
        Custom output directory, or ``None`` to use ``OUTPUT_DIR / <stem>``.
    log:
        Callback invoked for each log line  *(must be thread-safe)*.
    batch_index:
        Optional ``(current, total)`` tuple for batch progress display.

    Returns
    -------
    ``(success, output_md, output_dir, error)``
    """
    from mineru.cli.common import do_parse

    input_path = Path(file_path)
    file_stem = input_path.stem
    end_page = None if max_pages <= 0 else max_pages - 1

    setup_env(backend, device)

    # Determine output directory
    if output_dir_str is not None:
        out_dir = Path(output_dir_str)
    else:
        out_dir = OUTPUT_DIR / file_stem
    out_dir.mkdir(parents=True, exist_ok=True)
    temp_label = "_mineru_temp_"

    # ── Header ────────────────────────────────────────
    if batch_index:
        log(f"\n{'=' * 50}")
        log(f"[{batch_index[0]}/{batch_index[1]}] {input_path.name}")
        log(f"{'=' * 50}")
    log(f"File : {input_path.name}")
    log(f"Backend : {backend}")
    effective_device = "CUDA" if backend == "hybrid-auto-engine" else device.upper()
    log(f"Device  : {effective_device}")
    log(f"Language: {lang}")
    log("-" * 50)
    log("Processing, please wait...")

    # ── loguru sink to capture MinerU internal output ──
    def _sink(msg):
        line = msg.strip()
        if line:
            log(line)

    sink_id = _loguru.add(_sink, level="INFO", format="{message}", colorize=False)

    try:
        with open(file_path, "rb") as f:
            pdf_bytes = f.read()

        do_parse(
            output_dir=str(out_dir),
            pdf_file_names=[temp_label],
            pdf_bytes_list=[pdf_bytes],
            p_lang_list=[lang],
            backend=backend,
            parse_method=method,
            formula_enable=True,
            table_enable=True,
            f_draw_layout_bbox=False,
            f_draw_span_bbox=False,
            f_dump_md=True,
            f_dump_middle_json=False,
            f_dump_model_output=False,
            f_dump_orig_pdf=False,
            f_dump_content_list=False,
            start_page_id=0,
            end_page_id=end_page,
        )
    except Exception as e:  # noqa: BLE001 — do_parse may raise arbitrary errors
        log(f"\nERROR [{input_path.name}]: {e}")
        log(traceback.format_exc())
        return (False, None, None, str(e))
    finally:
        _loguru.remove(sink_id)

    # ── Flatten output ────────────────────────────────
    md_file = _find_md(out_dir, temp_label)
    if md_file is None:
        log(f"WARNING [{input_path.name}]: no Markdown output found.")
        return (False, None, None, "no Markdown output found")

    result_md = out_dir / f"{file_stem}.md"
    if result_md.exists():
        result_md.unlink()
    shutil.move(str(md_file), str(result_md))

    src_images = md_file.parent / "images"
    dst_images = out_dir / "images"
    if src_images.exists() and src_images.is_dir():
        if dst_images.exists():
            shutil.rmtree(str(dst_images))
        shutil.move(str(src_images), str(dst_images))

    temp_dir = out_dir / temp_label
    if temp_dir.exists():
        shutil.rmtree(str(temp_dir))

    # ── Orphaned image cleanup ────────────────────────
    md_raw = result_md.read_text(encoding="utf-8")
    img_dir = out_dir / "images"
    deleted = _clean_images(md_raw, img_dir)
    if deleted:
        log(f"Cleaned {deleted} unreferenced image(s)")

    # ── VLM image description ─────────────────────────
    if vlm_describe and backend == "hybrid-auto-engine" and img_dir.exists():
        log("Describing images with VLM...")
        try:
            described = _describe_images_in_md(result_md, img_dir, log)
            if described > 0:
                log(f"VLM described {described} image(s)")
        except Exception as e:  # noqa: BLE001 — VLM may raise arbitrary errors
            log(f"VLM image description failed: {e}")
            _logger.warning("VLM description error for %s: %s", result_md, e)

    # ── Done ──────────────────────────────────────────
    log(f"Output: {result_md}")
    log("-" * 50)
    log("Conversion complete")

    return (True, result_md, out_dir, None)


# ── VLM image description ──────────────────────────────


def _describe_images_in_md(
    md_path: Path, images_dir: Path, log: LogCallback
) -> int:
    """Describe each image in the markdown via the loaded VLM model.

    Accesses the ``ModelSingleton`` predictor that was already loaded by
    ``do_parse()``, calls ``content_extract()`` on each cropped image, and
    inserts the description as a blockquote below the image reference.

    Returns the number of images successfully described.
    """
    from mineru.backend.vlm.vlm_analyze import ModelSingleton
    from PIL import Image

    predictor = next(iter(ModelSingleton._models.values()), None)
    if predictor is None:
        log("No loaded VLM model found, skipping image description")
        return 0

    if not images_dir.exists():
        return 0

    md_content = md_path.read_text(encoding="utf-8")
    replacements: list[tuple[int, int, str]] = []
    described = 0

    for m in re.finditer(r"!\[([^\]]*)\]\(([^)]+)\)", md_content):
        _alt_text, rel_path_str = m.group(1), m.group(2)
        image_rel = Path(rel_path_str)
        image_path = (
            images_dir / image_rel.name
            if not image_rel.is_absolute()
            else image_rel
        )
        if not image_path.exists():
            continue
        try:
            pil_image = Image.open(image_path).convert("RGB")
            description = predictor.content_extract(pil_image, type="text")
            if description:
                old_ref = m.group(0)
                replacements.append((
                    m.start(),
                    m.end(),
                    f"{old_ref}\n> VLM description: {description.strip()}\n",
                ))
                described += 1
        except Exception as e:  # noqa: BLE001 — VLM may raise arbitrary errors
            _logger.warning("VLM failed on %s: %s", image_path, e)
            continue

    # Apply from end to start so earlier positions stay valid
    for start, end, new_text in reversed(replacements):
        md_content = md_content[:start] + new_text + md_content[end:]

    if described > 0:
        md_path.write_text(md_content, encoding="utf-8")

    return described
