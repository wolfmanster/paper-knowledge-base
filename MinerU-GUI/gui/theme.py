"""Theme setup for CTk-based MinerU GUI — Anthropic-inspired palette."""

from __future__ import annotations

from dataclasses import dataclass

import customtkinter as ctk

from app import get_dpi_scale


@dataclass(frozen=True)
class Palette:
    # Background layers — dark charcoal
    bg: str = "#171717"          # root / deepest background
    card_bg: str = "#1e1e1e"     # card / surface background
    input_bg: str = "#1a1a1a"    # input field background
    dropdown_bg: str = "#222222" # dropdown menu background

    # Text
    fg: str = "#e8e6e3"          # primary text — warm off-white
    text_secondary: str = "#8a877d"  # secondary / muted text
    text_muted: str = "#5c5a54"  # even more muted

    # Accent — Anthropic amber / warm ochre
    accent: str = "#c09865"
    accent_hover: str = "#b08855"
    accent_variant: str = "#3a3228"  # subtle accent tint for highlights
    accent_border: str = "#4a3f30"   # accent-tinted border

    # Functional
    success: str = "#6bbf5a"
    error: str = "#d9534f"
    error_bg: str = "#3d1f1f"

    # Borders
    border: str = "#2a2a2a"
    border_light: str = "#353535"

    # Log text area
    log_bg: str = "#181818"
    log_fg: str = "#d4d0c8"


PALETTE = Palette()
FONT = "Microsoft YaHei UI"

_dp_scale: float = 1.0


def heading_font(size: int = 15) -> tuple:
    return (FONT, size, "bold")


def bold_font(size: int = 12) -> tuple:
    return (FONT, size, "bold")


def text_font(size: int = 13) -> tuple:
    return (FONT, size)


def small_font(size: int = 11) -> tuple:
    return (FONT, size)


def mono_font(size: int = 12) -> tuple:
    return (FONT, size)


def accent_button(width: int = 100) -> dict:
    """Return kwargs for a primary accent button."""
    return {
        "fg_color": PALETTE.accent,
        "hover_color": PALETTE.accent_hover,
        "text_color": "#171717",
        "border_width": 0,
        "font": bold_font(12),
        "corner_radius": 6,
    }


def styled_entry(**overrides) -> dict:
    """Return kwargs for a CTkEntry with Anthropic input styling."""
    return {
        "fg_color": PALETTE.input_bg,
        "text_color": PALETTE.fg,
        "border_color": PALETTE.border,
        "border_width": 1,
        "font": text_font(13),
        "corner_radius": 6,
        **overrides,
    }


def ghost_button() -> dict:
    """Return kwargs for a ghost / outline button."""
    return {
        "fg_color": "transparent",
        "hover_color": "#2a2a2a",
        "text_color": PALETTE.text_secondary,
        "border_width": 1,
        "border_color": PALETTE.border,
        "font": text_font(12),
        "corner_radius": 6,
    }


def styled_option_menu(**overrides) -> dict:
    """Return kwargs for a CTkOptionMenu with Anthropic dropdown styling."""
    return {
        "fg_color": PALETTE.input_bg,
        "button_color": PALETTE.input_bg,
        "button_hover_color": "#2a2a2a",
        "text_color": PALETTE.fg,
        "font": text_font(13),
        "dropdown_fg_color": PALETTE.dropdown_bg,
        "dropdown_hover_color": "#333333",
        "dropdown_text_color": PALETTE.fg,
        "corner_radius": 6,
        "dynamic_resizing": False,
        **overrides,
    }


def card_frame() -> dict:
    """Return kwargs for a card-style frame."""
    return {
        "fg_color": PALETTE.card_bg,
        "corner_radius": 10,
        "border_width": 1,
        "border_color": PALETTE.border,
    }


def section_title(parent, text: str, **kwargs) -> ctk.CTkLabel:
    """Create a consistent section-title label with accent underline bar."""
    frame = ctk.CTkFrame(parent, fg_color="transparent")
    label = ctk.CTkLabel(
        frame,
        text=text,
        font=heading_font(15),
        text_color=PALETTE.fg,
        anchor="w",
        **kwargs,
    )
    label.pack(fill="x")
    # accent bar
    bar = ctk.CTkFrame(
        frame,
        fg_color=PALETTE.accent,
        height=2,
        corner_radius=1,
    )
    bar.pack(fill="x", pady=(2, 0))
    return frame


def init_dp_scale(root: ctk.CTk) -> None:
    """Initialize DPI scaling from a live root window."""
    global _dp_scale
    _dp_scale = get_dpi_scale(root)


def setup_ctk() -> None:
    """Configure CTk appearance mode and color theme (call before any CTk window)."""
    ctk.set_appearance_mode("Dark")
    ctk.set_default_color_theme("dark-blue")
