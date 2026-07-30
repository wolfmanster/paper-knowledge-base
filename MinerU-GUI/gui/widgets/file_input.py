"""File selection widget with multi-file support and folder selection."""

from __future__ import annotations

from pathlib import Path
from tkinter import StringVar, filedialog

import customtkinter as ctk

from app import SUPPORTED_EXTENSIONS
from gui.theme import PALETTE, ghost_button, styled_entry, text_font


def _format_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / 1024 / 1024:.1f} MB"


class FileInput(ctk.CTkFrame):
    def __init__(self, parent, **kwargs):
        super().__init__(parent, **kwargs)
        self._file_paths: list[str] = []
        self._build_ui()

    def _build_ui(self):
        # ── Browse row ──
        browse_row = ctk.CTkFrame(self, fg_color="transparent")
        browse_row.pack(fill="x", pady=(0, 8))

        self.path_entry = ctk.CTkEntry(
            browse_row,
            state="readonly",
            placeholder_text="拖拽或选择文件...",
            **styled_entry(),
        )
        self.path_entry.pack(side="left", fill="x", expand=True, padx=(0, 10))

        self.browse_btn = ctk.CTkButton(
            browse_row, text="选择文件...",
            command=self._browse_files, **ghost_button(), width=110,
        )
        self.browse_btn.pack(side="right", padx=(0, 6))

        self.folder_btn = ctk.CTkButton(
            browse_row, text="选择文件夹",
            command=self._browse_folder, **ghost_button(), width=110,
        )
        self.folder_btn.pack(side="right", padx=(0, 6))

        self.clear_btn = ctk.CTkButton(
            browse_row, text="清空",
            command=self.clear_files, **ghost_button(), width=70,
        )
        self.clear_btn.pack(side="right", padx=(0, 6))

        # ── File info ──
        self.info_var = StringVar(value="")
        self.info_label = ctk.CTkLabel(
            self, textvariable=self.info_var,
            text_color=PALETTE.text_secondary,
            font=text_font(12),
            anchor="w",
        )
        self.info_label.pack(anchor="w")

    def _browse_files(self):
        paths = filedialog.askopenfilenames(
            title="选择要解析的文档（可多选）",
            filetypes=[
                ("支持的文档", "*.pdf *.png *.jpg *.jpeg *.jp2 *.webp *.gif *.bmp *.tiff *.docx"),
                ("PDF 文件", "*.pdf"),
                ("图片文件", "*.png *.jpg *.jpeg *.jp2 *.webp *.gif *.bmp *.tiff"),
                ("Office 文件", "*.docx"),
                ("所有文件", "*.*"),
            ],
        )
        if paths:
            self._file_paths = [str(Path(p).resolve()) for p in paths]
            self._update_display()

    def _browse_folder(self):
        folder = filedialog.askdirectory(title="选择包含文档的文件夹")
        if folder:
            folder_path = Path(folder).resolve()
            paths = sorted(
                str(p) for p in folder_path.iterdir()
                if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS
            )
            if paths:
                self._file_paths = paths
                self._update_display()
            else:
                self._file_paths.clear()
                self._update_display()

    def set_files(self, paths: list[str]):
        self._file_paths = list(dict.fromkeys(paths))
        self._update_display()

    def add_files(self, paths: list[str]):
        for p in paths:
            resolved = str(Path(p).resolve())
            if resolved not in self._file_paths:
                self._file_paths.append(resolved)
        self._update_display()

    def clear_files(self):
        self._file_paths.clear()
        self._update_display()

    def get_file_paths(self) -> list[str]:
        return self._file_paths.copy()

    def _update_display(self):
        n = len(self._file_paths)
        if n == 0:
            self.path_entry.configure(state="normal")
            self.path_entry.delete(0, "end")
            self.path_entry.configure(state="readonly", placeholder_text="拖拽或选择文件...")
            self.info_var.set("")
            return

        if n == 1:
            p = Path(self._file_paths[0])
            self.path_entry.configure(state="normal")
            self.path_entry.delete(0, "end")
            self.path_entry.insert(0, str(p))
            self.path_entry.configure(state="readonly")

            parts = [p.name]
            try:
                size = p.stat().st_size
                parts.append(_format_size(size))
            except OSError:
                pass
            if p.suffix.lower() == ".pdf":
                try:
                    import pypdf
                    with open(p, "rb") as f:
                        reader = pypdf.PdfReader(f)
                    parts.append(f"{len(reader.pages)} 页")
                except ImportError:
                    pass
                except (pypdf.errors.PdfReadError, OSError):
                    pass
            self.info_var.set("  ·  ".join(parts))
        else:
            self.path_entry.configure(state="normal")
            self.path_entry.delete(0, "end")
            self.path_entry.insert(0, f"[{n} 个文件]")
            self.path_entry.configure(state="readonly")

            total_size = 0
            for fp in self._file_paths:
                try:
                    total_size += Path(fp).stat().st_size
                except OSError:
                    pass
            self.info_var.set(f"已选择 {n} 个文件，总计 {_format_size(total_size)}")
