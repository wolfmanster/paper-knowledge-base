"""Log viewer with progress bar and status display — Anthropic styled."""

from __future__ import annotations

import time
from queue import Empty

import customtkinter as ctk

from gui._core import parse_log_for_progress
from gui.theme import PALETTE, mono_font, small_font


class LogViewer(ctk.CTkFrame):
    def __init__(self, parent, on_done_callback, **kwargs):
        super().__init__(parent, **kwargs)
        self._on_done = on_done_callback
        self._log_queue = None
        self._done_event = None
        self._start_time: float | None = None

        # Indeterminate animation state
        self._indeterminate_timer = None
        self._indeterminate_running = False
        self._indeterminate_pos = 0.0
        self._indeterminate_dir = 1

        self._build_ui()

    def _build_ui(self):
        # ── Progress bar ──
        self.progress = ctk.CTkProgressBar(self, height=6, corner_radius=3)
        self.progress.pack(fill="x", pady=(0, 6))
        self.progress.set(0.0)

        progress_info = ctk.CTkFrame(self, fg_color="transparent")
        progress_info.pack(fill="x", pady=(0, 8))

        self.progress_pct = ctk.CTkLabel(
            progress_info, text="",
            font=small_font(12),
            text_color=PALETTE.accent,
        )
        self.progress_pct.pack(side="left")

        self.status_text = ctk.CTkLabel(
            progress_info, text="就绪",
            font=small_font(12),
            text_color=PALETTE.text_secondary,
        )
        self.status_text.pack(side="right")

        # ── Log text ──
        self.log_text = ctk.CTkTextbox(
            self,
            wrap="word",
            height=240,
            font=mono_font(12),
            fg_color=PALETTE.log_bg,
            text_color=PALETTE.log_fg,
            border_width=1,
            border_color=PALETTE.border,
        )
        self.log_text.pack(fill="both", expand=True)
        self.log_text.configure(state="disabled")

    # ── Queue setup ──

    def set_queue(self, log_queue, done_event):
        self._log_queue = log_queue
        self._done_event = done_event

    # ── Indeterminate animation ──

    def _start_indeterminate(self):
        self._indeterminate_running = True
        self._indeterminate_pos = 0.0
        self._indeterminate_dir = 1
        self._indeterminate_tick()

    def _stop_indeterminate(self):
        self._indeterminate_running = False
        if self._indeterminate_timer:
            self.after_cancel(self._indeterminate_timer)
            self._indeterminate_timer = None

    def _indeterminate_tick(self):
        if not self._indeterminate_running:
            return
        step = 0.03
        self._indeterminate_pos += step * self._indeterminate_dir
        if self._indeterminate_pos >= 1.0:
            self._indeterminate_pos = 1.0
            self._indeterminate_dir = -1
        elif self._indeterminate_pos <= 0.0:
            self._indeterminate_pos = 0.0
            self._indeterminate_dir = 1
        self.progress.set(self._indeterminate_pos)
        self._indeterminate_timer = self.after(50, self._indeterminate_tick)

    # ── Public API ──

    def start(self):
        self._start_time = time.time()
        self._start_indeterminate()
        self.progress_pct.configure(text="")
        self.status_text.configure(text="处理中...", text_color=PALETTE.accent)
        self._poll()

    def stop(self, success: bool = True):
        self._stop_indeterminate()
        self.progress.set(1.0)
        self.progress_pct.configure(text="100%")
        if success:
            self.status_text.configure(
                text=f"完成 ({self._elapsed()})",
                text_color=PALETTE.success,
            )
        else:
            self.status_text.configure(
                text="出错",
                text_color=PALETTE.error,
            )

    def reset(self):
        self._stop_indeterminate()
        self.progress.set(0.0)
        self.progress_pct.configure(text="")
        self.status_text.configure(text="就绪", text_color=PALETTE.text_secondary)

    def clear_log(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def append_log(self, text: str):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    # ── Internal ──

    def _elapsed(self) -> str:
        if self._start_time is None:
            return "0s"
        secs = int(time.time() - self._start_time)
        if secs < 60:
            return f"{secs}s"
        return f"{secs // 60}m {secs % 60}s"

    def _poll(self):
        if self._log_queue is None:
            return

        batch: list[str] = []
        try:
            while True:
                batch.append(self._log_queue.get_nowait())
        except Empty:
            pass

        if batch:
            self.log_text.configure(state="normal")
            for line in batch:
                self.log_text.insert("end", line)
                result = parse_log_for_progress(line)
                if result:
                    cur, total = result
                    if total > 0:
                        self._stop_indeterminate()
                        self.progress.set(cur / total)
                        self.progress_pct.configure(text=f"{int(cur * 100 / total)}%")
                    self.status_text.configure(
                        text=f"第 {cur}/{total} 页 ({self._elapsed()})",
                        text_color=PALETTE.fg,
                    )
            self.log_text.see("end")
            self.log_text.configure(state="disabled")

        if self._done_event and self._done_event.is_set():
            self.stop(success=True)
            self._on_done()
            return

        self.after(50, self._poll)
