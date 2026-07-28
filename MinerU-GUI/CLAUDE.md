# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

MinerU GUI — CustomTkinter desktop GUI for MinerU document parsing (PDF/images/Office → Markdown). Calls `mineru.cli.common.do_parse` directly via Python API.

- **GUI**: CustomTkinter 5.2.2 (no web server or ports)
- **Python**: 3.13 (venv at `C:\Users\70918\.venv\`)
- **Engine**: MinerU 3.0.4
- **Theme**: Anthropic-inspired dark/amber palette in `gui/theme.py` (`Palette` dataclass)
- **Font**: Microsoft YaHei UI, centralized in `gui/theme.py` with helpers:
  `heading_font(size)`, `bold_font(size)`, `text_font(size)`, `small_font(size)`, `mono_font(size)`
- **Window title**: `"MinerU"`
- **Drag-drop**: `windnd` via `WM_DROPFILES` — handler only appends to a buffer, CTk event loop polls every 150ms

## Start

```bat
:: 首次: setup.bat → 创建 .venv + 安装 GUI 依赖
:: 还需手动安装 mineru: .venv\Scripts\pip install mineru[core]
:: 之后: 双击 start.bat
```

`start.bat` 自动发现项目目录下的 `.venv\Scripts\python.exe` — 不依赖全局 Python。

## Validation

```powershell
# Syntax check all modules
python -m py_compile app.py gui.py gui\_core.py gui\theme.py gui\worker.py
python -m py_compile gui\widgets\file_input.py gui\widgets\params_panel.py gui\widgets\log_viewer.py
python -m py_compile mineru_api.py

# Lint and type check
ruff check .
mypy . --ignore-missing-imports

# Full import check (launches the window — close it to complete)
python gui.py
```

## Architecture

### Data Flow

```
User selects/drags file(s) → MainWindow._start_convert()
  → start_batch_conversion() → daemon thread
  → gui._core.run_core(): set env vars → do_parse() → find_md() → flatten temp dirs → clean_orphaned_images()
  → (optional) _describe_images_in_md(): VLM describes each image via MinerUClient.content_extract()
  → log_viewer._poll() every 50ms reads queue.Queue to update UI
  → threading.Event triggers _on_conversion_done() to re-enable buttons
```

### Shared Core (`gui/_core.py`)

The actual conversion logic lives in ``gui/_core.py`` and is shared between:

| Consumer | Mechanism | Output routing |
|----------|-----------|----------------|
| ``gui/worker.py``  | daemon thread + ``queue.Queue`` | CTk log viewer |
| ``mineru_api.py``  | direct synchronous call | ``ConversionResult.log_lines`` |

``gui/_core.run_core()`` accepts a ``log: Callable[[str], None]`` callback so
each consumer can route log output to its own destination without any
dependency on CTk or queue.

### Threading

**Producer** (``gui._core.run_core`` in daemon thread) → loguru sink → ``queue.Queue``
**Consumer** (``log_viewer._poll`` main thread) → 50ms ``after()`` timer polling

`queue.Queue` carries both log lines and MinerU progress output. `_done_event` (threading.Event) signals completion from worker to UI.

### Drag-drop (Thread-safe Buffer Pattern)

`windnd.hook_dropfiles()` registers a Windows message hook. The handler must NOT touch CTk — it only appends file tuples to `_drop_pending: list[tuple[str, ...]]`. A 150ms `after()` timer (`_poll_drop`) drains the buffer on the main CTk thread and calls `_process_drop()` to filter supported extensions and add to `FileInput`.

Apply this buffer pattern to any Windows message callback that needs to update CTk state.

### Window Geometry & Params Persistence

`MainWindow._save_geometry()` writes `root.geometry()` plus current parse `params` to `.window_geometry.json` on close and on drag-resize (500ms debounce via `after()`). `_restore_geometry()` reads geometry and stores params for restoration via `_restore_params()` after `_build_ui()`. Falls back to 960x780 if missing.

### Progress Bar Animation

CTkProgressBar has no built-in indeterminate mode. `LogViewer._start_indeterminate()` oscillates `self.progress.set(pos)` between 0.0 and 1.0 via a 50ms `after()` timer (`_indeterminate_tick`). When `parse_log_for_progress()` extracts `page N/M` from a log line, the timer is cancelled and progress switches to determinate mode.

### Page Progress Parsing

`gui._core.parse_log_for_progress()` uses regex `page\s*(\d+)\s*/\s*(\d+)` (case-insensitive) to extract `(current_page, total_pages)` from MinerU's output lines. This is the only signal for determinate progress — if the engine doesn't emit it, indeterminate mode runs the whole time.

### DPI Scaling

`gui.py` sets Per-Monitor DPI awareness via `ctypes` before any CTk init. `app.get_dpi_scale()` reads the actual scale factor from `tk.winfo_fpixels("1i")`. Font sizes are handled natively since DPI awareness is enabled.

### Backend Environment Variables

`gui._core.setup_env()` merges the condition `backend == "hybrid-auto-engine" or device == "gpu"` to set CUDA path, with a single `os.cpu_count()` call for CPU path:

| Env Var | Pipeline CPU | Pipeline GPU | Hybrid-Auto-Engine (GPU) |
|---------|-------------|-------------|--------------------------|
| `MINERU_DEVICE_MODE` | `cpu` | `cuda` | `cuda` |
| `MINERU_PDF_RENDER_THREADS` | `os.cpu_count()` | `1` | `1` |
| `MINERU_VIRTUAL_VRAM_SIZE` | `32` | — | — |
| `OMP_NUM_THREADS` | `os.cpu_count()` | — | — |

### VLM Image Description (Post-processing)

When the user checks "VLM 详细描述图片" (only available for `hybrid-auto-engine`), ``gui._core._describe_images_in_md()`` runs after ``do_parse()`` completes:

1. Reads the generated Markdown, finds all `![]()` image references
2. Accesses the MinerUClient loaded during parsing via `ModelSingleton._models` (the VLM model is a process-global singleton)
3. Calls `predictor.content_extract(image, type="text")` on each cropped image
4. Inserts the description as a blockquote below the image in the MD file

**Important:** The VLM model must already be loaded by `do_parse()`. This function only works after a successful hybrid engine parse. The `ModelSingleton` uses `threading.RLock` internally. Descriptions are inserted position-safe (reverse-order replacement).

### Post-processing

`_clean_orphaned_images(content, images_dir)` — scans MD for `![]()` references, deletes unreferenced images from the `images/` folder. MinerU sometimes writes both text and image for table regions, leaving orphans.

### Batch Processing

`FileInput` stores `_file_paths: list[str]`. Conversion always delegates to `start_batch_conversion()` → `_run_batch_conversion()`, which loops over files with `[i/N]` progress (works for both 1 and N files), prints success/fail summary at end.

### Parse Parameters

| Parameter | Options | Default |
|-----------|---------|---------|
| Backend | pipeline / hybrid-auto-engine | hybrid-auto-engine |
| OCR Language | ch, en, korean, japan, +13 more | ch |
| Method | auto / txt / ocr | auto |
| Device | cpu / gpu | gpu (fixed to gpu for hybrid) |
| Max Pages | 0-1000 | 0 (all) |
| VLM Describe | True / False | False (hybrid only) |

Formula and table recognition are hardcoded: `formula_enable=True, table_enable=True`.

Device menu is disabled when `hybrid-auto-engine` is selected. `_on_backend_change()` is called in `__init__` to set initial disabled state. The VLM describe checkbox is similarly disabled for non-hybrid backends.

### Theme System

All UI colors come from the `PALETTE` object (frozen dataclass `Palette`) in `gui/theme.py`. Helper functions that return pre-configured kwargs dicts for consistent styling:

| Helper | Purpose |
|--------|---------|
| `accent_button()` | Primary amber action button |
| `ghost_button()` | Outline button with transparent bg |
| `card_frame()` | Card container frame (rounded + border) |
| `section_title()` | Label with amber underline bar |
| `styled_option_menu()` | Dark CTkOptionMenu with custom dropdown |
| `styled_entry()` | Dark CTkEntry with border |

To change a color, edit the `Palette` dataclass — no need to touch widget files. To add a new widget style, add a factory function here.

## Key Conventions

- All files use `from __future__ import annotations` for PEP 604 style annotations.
- `app.py` is the central constants hub (`PROJECT_DIR`, `OUTPUT_DIR`, `LANGUAGES`, `BACKENDS`, `SUPPORTED_EXTENSIONS`). Other files import from `app` rather than redefining lists.
- Use `Path` objects from `pathlib`, not raw strings, for path manipulation.
- Error output includes `[{filename}]` prefix to identify which file failed in batch.
- `gui/__init__.py` and `gui/widgets/__init__.py` are empty package markers.
- `pypdf` is an optional dependency — usage is guarded by `ImportError` in `file_input.py`.

## File Layout

```
pdf/
├── app.py                  # Constants + utility functions (DPI, orphan cleaning, md lookup)
├── gui.py                  # Entry point: DPI awareness → setup_ctk() → MainWindow
├── mineru_api.py           # Standalone Python API (可被外部项目调用)
├── pyproject.toml          # pip install 支持
├── gui/
│   ├── _core.py            # Shared conversion core (run_core, setup_env, VLM)
│   ├── theme.py            # Palette, font helpers, widget style factories
│   ├── main_window.py      # Header bar + 3 cards + action buttons, windnd, geometry persistence
│   ├── widgets/
│   │   ├── file_input.py   # File path entry, browse/file/folder buttons, drag-add
│   │   ├── params_panel.py # CTkOptionMenu rows + validated pages Entry + VLM checkbox
│   │   └── log_viewer.py   # CTkTextbox + CTkProgressBar + indeterminate animation
│   └── worker.py           # Daemon thread: do_parse(), loguru sink → queue, VLM post-processing
├── start.bat               # CMD launcher (ANSI/GBK encoding required)
├── output/                 # Parse output directory
└── .window_geometry.json   # Auto-saved geometry + last-used params
```

## Python API (`mineru_api.py`)

该模块提取了 GUI 的核心转换逻辑，移除了对 CustomTkinter 的依赖，可在外部项目中直接调用。

### 在其他项目中调用

```python
import sys
sys.path.insert(0, r"path/to/MinerU GUI")
from mineru_api import convert_document, batch_convert

# 单个文件
result = convert_document("report.pdf")
if result.success:
    print(f"输出: {result.output_md}")         # Path("output/report/report.md")
    print(f"日志: {result.log_lines}")

# 批量
results = batch_convert(["a.pdf", "b.pdf", "c.png"],
                         backend="pipeline", device="cpu")
for r in results:
    print(f"{'✅' if r.success else '❌'} {r.file_path}: {r.output_md}")

# 自定义输出目录
result = convert_document("doc.pdf", output_dir="./my_output/doc")
```

### pip 安装

```bash
pip install -e /path/to/MinerU\ GUI
# 之后可以直接:
from mineru_api import convert_document
```

### API 参考

`convert_document(file_path, backend, lang, method, max_pages, device, vlm_describe, output_dir)` → `ConversionResult`

`batch_convert(file_paths, ...)` → `list[ConversionResult]`

`ConversionResult` 字段: `success`, `file_path`, `output_md`, `output_dir`, `log_lines`, `error`

**线程安全**: 环境变量是进程级别的全局状态，`convert_document` 不是线程安全的。如需并发，请在 `multiprocessing` 子进程中调用。

## Notes

- Run `gui.py` directly (no `__main__` guard — `gui.py` uses `if __name__ == "__main__"`).
- `setup_ctk()` in `gui/theme.py` must be called **before** `from gui.main_window import MainWindow` (already done in `gui.py`).
- `mineru_api.py` 是独立的，不需要 `setup_ctk()`。直接 import 即可。
- `start.bat` and `setup.bat` must use **ANSI/GBK** encoding — CMD doesn't read UTF-8.
- `requirements.txt` only lists GUI deps (`customtkinter`, `windnd`, `loguru`). `mineru` must be installed separately.
- `output/` dir is auto-created by `app.py` at import time via `OUTPUT_DIR.mkdir(exist_ok=True)`.
- Add/remove backends → edit `app.py` `BACKENDS` list.
- Add/remove languages → edit `app.py` `LANGUAGES` list.
- To add a new param field: create a `StringVar`/`BooleanVar` in `ParamsPanel.__init__`, add a widget row in `_build_ui`, extend `get_params()` / `set_params()`, thread the value through `main_window.py` and `worker.py`.
- Python code review → `python-reviewer` agent. GPU/torch issues → `mineru-troubleshooter` agent.
