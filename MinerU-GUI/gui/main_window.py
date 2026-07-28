"""Main window — Anthropic-styled card-based layout."""

from __future__ import annotations

import os
import queue
import threading
from pathlib import Path

import customtkinter as ctk
import windnd  # type: ignore

from app import OUTPUT_DIR, PROJECT_DIR, SUPPORTED_EXTENSIONS, __version__ as APP_VERSION
from gui.theme import (
    PALETTE,
    accent_button,
    bold_font,
    card_frame,
    ghost_button,
    init_dp_scale,
    section_title,
    text_font,
)
from gui.widgets.file_input import FileInput
from gui.widgets.log_viewer import LogViewer
from gui.widgets.params_panel import ParamsPanel
from gui.worker import init_worker, start_batch_conversion


class MainWindow:
    _GEOMETRY_FILE = PROJECT_DIR / ".window_geometry.json"

    def __init__(self):
        self.root = ctk.CTk()
        self.root.title(f"MinerU {APP_VERSION}")

        # Init DPI scaling with the live root
        init_dp_scale(self.root)

        # Restore previous window geometry
        self._restore_geometry()

        min_w, min_h = 720, 600
        self.root.minsize(min_w, min_h)

        self.is_running = False
        self._log_queue = queue.Queue()
        self._done_event = threading.Event()

        # Apply background
        self.root.configure(fg_color=PALETTE.bg)

        # Init worker
        init_worker(self._log_queue, self._done_event)

        self._build_ui()
        self._restore_params()

        # windnd — synchronous handler, only appends to buffer
        self._drop_pending: list[tuple[str, ...]] = []

        def _handler(files):
            self._drop_pending.append(tuple(files))

        windnd.hook_dropfiles(self.root.winfo_id(), _handler, force_unicode=True)

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._resize_timer = None
        self.root.bind("<Configure>", self._on_configure)

        # Poll for pending drops every 150ms
        self.root.after(150, self._poll_drop)

    def _poll_drop(self):
        if self._drop_pending:
            for files_tuple in self._drop_pending:
                self._process_drop(list(files_tuple))
            self._drop_pending.clear()
        self.root.after(150, self._poll_drop)

    def _build_ui(self):
        # ── Header bar ──
        self._build_header()

        # ── Main content area ──
        main = ctk.CTkFrame(self.root, fg_color="transparent")
        main.pack(fill="both", expand=True, padx=20, pady=(0, 16))

        # ── File input card ──
        card_args = card_frame()
        self.file_card = ctk.CTkFrame(main, **card_args)
        self.file_card.pack(fill="x", pady=(0, 10))

        title = section_title(self.file_card, "选择文件")
        title.pack(fill="x", padx=14, pady=(12, 2))

        self.file_input = FileInput(self.file_card, fg_color="transparent")
        self.file_input.pack(fill="x", padx=14, pady=(4, 14))

        # ── Parameters card ──
        self.params_card = ctk.CTkFrame(main, **card_args)
        self.params_card.pack(fill="x", pady=(0, 10))

        title2 = section_title(self.params_card, "解析参数")
        title2.pack(fill="x", padx=14, pady=(12, 2))

        self.params = ParamsPanel(self.params_card, fg_color="transparent")
        self.params.pack(fill="x", padx=14, pady=(4, 14))

        # ── Log viewer card ──
        self.log_card = ctk.CTkFrame(main, **card_args)
        self.log_card.pack(fill="both", expand=True, pady=(0, 10))

        title3 = section_title(self.log_card, "处理日志")
        title3.pack(fill="x", padx=14, pady=(12, 2))

        self.log_viewer = LogViewer(self.log_card, on_done_callback=self._on_conversion_done)
        self.log_viewer.pack(fill="both", expand=True, padx=14, pady=(4, 14))
        self.log_viewer.set_queue(self._log_queue, self._done_event)

        # ── Action bar ──
        action_frame = ctk.CTkFrame(main, fg_color="transparent")
        action_frame.pack(fill="x", pady=(4, 0))

        self.convert_btn = ctk.CTkButton(
            action_frame,
            text="开始转换",
            command=self._start_convert,
            **accent_button(140),
        )
        self.convert_btn.pack(side="left", padx=(0, 8))

        self.open_btn = ctk.CTkButton(
            action_frame,
            text="打开输出文件夹",
            command=self._open_output,
            state="disabled",
            **ghost_button(),
        )
        self.open_btn.pack(side="left", padx=(0, 8))

        ctk.CTkButton(
            action_frame,
            text="清空日志",
            command=self._clear_log,
            **ghost_button(),
        ).pack(side="left")

    def _build_header(self):
        """Anthropic-style header bar."""
        header = ctk.CTkFrame(self.root, fg_color=PALETTE.card_bg, corner_radius=0, height=48)
        header.pack(fill="x")
        header.pack_propagate(False)

        container = ctk.CTkFrame(header, fg_color="transparent")
        container.pack(fill="x", padx=20, pady=0)

        # "MinerU" logo text
        ctk.CTkLabel(
            container,
            text="MinerU",
            font=bold_font(16),
            text_color=PALETTE.accent,
        ).pack(side="left")

        ctk.CTkLabel(
            container,
            text="文档解析工具",
            font=text_font(12),
            text_color=PALETTE.text_muted,
        ).pack(side="left", padx=(10, 0))

    # ── Window geometry persistence ──

    def _restore_geometry(self):
        try:
            import json
            if self._GEOMETRY_FILE.exists():
                data = json.loads(self._GEOMETRY_FILE.read_text(encoding="utf-8"))
                geo = data.get("geometry")
                if geo:
                    self.root.geometry(geo)
                self._saved_params = data.get("params")
                return
        except (json.JSONDecodeError, OSError, KeyError):
            pass
        self._saved_params = None
        w, h = 960, 780
        self.root.geometry(f"{w}x{h}")

    def _restore_params(self):
        if self._saved_params:
            self.params.set_params(self._saved_params)

    def _save_geometry(self):
        try:
            import json
            params_dict = self.params.get_params()
            self._GEOMETRY_FILE.write_text(
                json.dumps(
                    {"geometry": self.root.geometry(), "params": params_dict},
                ),
                encoding="utf-8",
            )
        except (OSError, TypeError):
            pass

    # ── Actions ──

    def _start_convert(self):
        if self.is_running:
            return

        file_paths = self.file_input.get_file_paths()
        if not file_paths:
            self.log_viewer.append_log("请先选择文件或文件夹\n")
            return

        self.is_running = True
        self.convert_btn.configure(text="转换中...", state="disabled")
        self.open_btn.configure(state="disabled")
        self.log_viewer.clear_log()
        self._done_event.clear()

        params = self.params.get_params()
        self.log_viewer.start()

        start_batch_conversion(
            file_paths=file_paths,
            backend=params["backend"],
            lang=params["lang"],
            method=params["method"],
            max_pages=params["max_pages"],
            device=params["device"],
            vlm_describe=params.get("vlm_describe", False),
        )

    def _on_conversion_done(self):
        self.is_running = False
        self.convert_btn.configure(text="开始转换", state="normal")
        self.open_btn.configure(state="normal")

    def _clear_log(self):
        self.log_viewer.clear_log()
        self.log_viewer.reset()

    def _open_output(self):
        try:
            os.startfile(str(OUTPUT_DIR.resolve()))
        except OSError as e:
            self.log_viewer.append_log(f"无法打开输出文件夹: {e}\n")

    def _on_close(self):
        self.is_running = False
        self._save_geometry()
        self.root.destroy()

    def _on_configure(self, event):
        if event.widget is self.root and self.root.state() == "normal":
            if self._resize_timer is not None:
                self.root.after_cancel(self._resize_timer)
            self._resize_timer = self.root.after(500, self._save_geometry)

    def _process_drop(self, files: list):
        supported = []
        for f in files:
            p = Path(f)
            if p.is_dir():
                for child in sorted(p.iterdir()):
                    if child.is_file() and child.suffix.lower() in SUPPORTED_EXTENSIONS:
                        supported.append(str(child.resolve()))
            elif p.suffix.lower() in SUPPORTED_EXTENSIONS:
                supported.append(str(p.resolve()))
        if supported:
            self.file_input.add_files(supported)

    def run(self):
        self.root.mainloop()
