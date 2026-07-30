"""Parameters panel for conversion options."""

from __future__ import annotations

from tkinter import BooleanVar, StringVar

import customtkinter as ctk

from app import BACKENDS, LANGUAGES
from gui.theme import PALETTE, styled_entry, styled_option_menu, text_font

METHODS = ["auto", "txt", "ocr"]


def _make_label(parent: ctk.CTkBaseClass, text: str, width: int | None = None) -> ctk.CTkLabel:
    kwargs: dict = {
        "text": text,
        "font": text_font(13),
        "text_color": PALETTE.fg,
        "anchor": "w",
    }
    if width is not None:
        kwargs["width"] = width
    return ctk.CTkLabel(parent, **kwargs)


class ParamsPanel(ctk.CTkFrame):
    def __init__(self, parent, **kwargs):
        super().__init__(parent, **kwargs)
        self.backend_var = StringVar(value="hybrid-auto-engine")
        self.lang_var = StringVar(value="ch")
        self.method_var = StringVar(value="auto")
        self.max_pages_var = StringVar(value="0")
        self.device_var = StringVar(value="gpu")
        self.vlm_describe_var = BooleanVar(value=False)
        self._build_ui()
        self._on_backend_change()

    def _build_ui(self):
        # ── Row 1: backend + language ──
        row1 = ctk.CTkFrame(self, fg_color="transparent")
        row1.pack(fill="x", pady=(0, 10))

        _make_label(row1, "解析后端", 70).pack(side="left")
        self.backend_combo = ctk.CTkOptionMenu(
            row1,
            variable=self.backend_var,
            values=BACKENDS,
            command=self._on_backend_change,
            width=200,
            **styled_option_menu(),
        )
        self.backend_combo.pack(side="left", padx=(8, 24))

        _make_label(row1, "OCR 语言", 70).pack(side="left")
        self.lang_combo = ctk.CTkOptionMenu(
            row1,
            variable=self.lang_var,
            values=LANGUAGES,
            width=160,
            **styled_option_menu(),
        )
        self.lang_combo.pack(side="left", padx=(8, 0))

        # ── Row 2: method + device + max_pages ──
        row2 = ctk.CTkFrame(self, fg_color="transparent")
        row2.pack(fill="x")

        _make_label(row2, "解析方法", 70).pack(side="left")
        self.method_combo = ctk.CTkOptionMenu(
            row2,
            variable=self.method_var,
            values=METHODS,
            width=130,
            **styled_option_menu(),
        )
        self.method_combo.pack(side="left", padx=(8, 24))

        _make_label(row2, "设备", 40).pack(side="left")
        self.device_combo = ctk.CTkOptionMenu(
            row2,
            variable=self.device_var,
            values=["cpu", "gpu"],
            width=90,
            **styled_option_menu(),
        )
        self.device_combo.pack(side="left", padx=(8, 24))

        _make_label(row2, "最大页数").pack(side="left")
        self.pages_entry = ctk.CTkEntry(row2, width=80, textvariable=self.max_pages_var, **styled_entry())
        self.pages_entry.pack(side="left", padx=(8, 6))
        self.pages_entry.bind("<KeyRelease>", self._on_pages_key)
        self.pages_entry.bind("<FocusOut>", self._clamp_pages)

        ctk.CTkLabel(
            row2, text="(0=全部)",
            font=text_font(12),
            text_color=PALETTE.text_secondary,
        ).pack(side="left")

        # ── Row 3: VLM image description checkbox ──
        row3 = ctk.CTkFrame(self, fg_color="transparent")
        row3.pack(fill="x", pady=(12, 0))

        self.vlm_describe_cb = ctk.CTkCheckBox(
            row3,
            variable=self.vlm_describe_var,
            text="VLM 详细描述图片（仅混合模式）",
            font=text_font(13),
            text_color=PALETTE.fg,
            fg_color=PALETTE.accent,
            hover_color=PALETTE.accent_hover,
            checkmark_color=PALETTE.bg,
            border_color=PALETTE.border,
            border_width=2,
            corner_radius=4,
        )
        self.vlm_describe_cb.pack(side="left")

    # ── Validation ──

    def _on_pages_key(self, event):
        val = self.max_pages_var.get()
        if val and not val.isdigit():
            self.max_pages_var.set("".join(filter(str.isdigit, val)))

    def _clamp_pages(self, event=None):
        try:
            val = int(self.max_pages_var.get() or "0")
        except ValueError:
            val = 0
        val = max(0, min(val, 1000))
        self.max_pages_var.set(str(val))

    # ── Backend change handler ──

    def _on_backend_change(self, choice=None):
        is_hybrid = self.backend_var.get() == "hybrid-auto-engine"
        if is_hybrid:
            self.device_combo.configure(state="disabled")
            self.device_var.set("gpu")
        else:
            self.device_combo.configure(state="normal")

        # VLM describe only makes sense for hybrid-auto-engine
        if not is_hybrid:
            self.vlm_describe_cb.configure(state="disabled")
            self.vlm_describe_var.set(False)
        else:
            self.vlm_describe_cb.configure(state="normal")

    # ── Serialization ──

    def get_params(self) -> dict:
        return {
            "backend": self.backend_var.get(),
            "lang": self.lang_var.get(),
            "method": self.method_var.get(),
            "max_pages": int(self.max_pages_var.get() or "0"),
            "device": self.device_var.get(),
            "vlm_describe": self.vlm_describe_var.get(),
        }

    def set_params(self, params: dict) -> None:
        _key_map = {
            "backend": self.backend_var,
            "lang": self.lang_var,
            "method": self.method_var,
            "device": self.device_var,
        }
        for key, var in _key_map.items():
            if key in params:
                var.set(params[key])
        if "max_pages" in params:
            self.max_pages_var.set(str(params["max_pages"]))
        if "vlm_describe" in params:
            self.vlm_describe_var.set(params["vlm_describe"])
        self._on_backend_change()
