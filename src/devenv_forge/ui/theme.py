"""Colour palette and stylesheet, following the Windows light/dark setting."""

from __future__ import annotations

import sys
from dataclasses import dataclass

from ..core.models import Status


@dataclass(frozen=True, slots=True)
class Palette:
    bg: str
    surface: str
    surface_alt: str
    border: str
    text: str
    muted: str
    accent: str
    accent_text: str
    ok: str
    warn: str
    danger: str
    info: str
    neutral: str


DARK = Palette(
    bg="#1b1d21",
    surface="#24262b",
    surface_alt="#2c2f35",
    border="#3a3e45",
    text="#e6e8ea",
    muted="#9aa0a8",
    accent="#4f8cff",
    accent_text="#ffffff",
    ok="#3fb950",
    warn="#d29922",
    danger="#f85149",
    info="#58a6ff",
    neutral="#7d848d",
)

LIGHT = Palette(
    bg="#f4f5f7",
    surface="#ffffff",
    surface_alt="#eef0f3",
    border="#d5d9de",
    text="#1c1f23",
    muted="#5c646d",
    accent="#2563eb",
    accent_text="#ffffff",
    ok="#1a7f37",
    warn="#9a6700",
    danger="#cf222e",
    info="#0969da",
    neutral="#6b727b",
)


def windows_prefers_dark() -> bool:
    """Read the Windows apps theme preference; default to light elsewhere."""
    if sys.platform != "win32":
        return False
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        ) as key:
            return int(winreg.QueryValueEx(key, "AppsUseLightTheme")[0]) == 0
    except (OSError, ValueError):
        return False


def active_palette() -> Palette:
    return DARK if windows_prefers_dark() else LIGHT


def rgba(hex_color: str, alpha: float) -> str:
    """Build an rgba() string for a stylesheet.

    Qt reads an 8-digit hex literal as #AARRGGBB, not the CSS #RRGGBBAA, so
    appending an alpha pair to a colour silently produces a different hue.
    Always express translucency through this helper.
    """
    raw = hex_color.lstrip("#")
    red, green, blue = (int(raw[i : i + 2], 16) for i in (0, 2, 4))
    return f"rgba({red}, {green}, {blue}, {alpha:.3f})"


def status_color(status: Status, palette: Palette) -> str:
    return {
        Status.OK: palette.ok,
        Status.INFO: palette.info,
        Status.SKIPPED: palette.neutral,
        Status.REPAIRABLE: palette.warn,
        Status.MISSING: palette.danger,
        Status.UNSUPPORTED: palette.danger,
        Status.FAILED: palette.danger,
    }.get(status, palette.neutral)


STATUS_LABEL = {
    Status.OK: "Ready",
    Status.INFO: "Note",
    Status.SKIPPED: "Skipped",
    Status.REPAIRABLE: "Needs repair",
    Status.MISSING: "Missing",
    Status.UNSUPPORTED: "Unsupported",
    Status.FAILED: "Check failed",
}


def build_stylesheet(p: Palette) -> str:
    return f"""
QWidget {{
    background: {p.bg};
    color: {p.text};
    font-family: "Segoe UI Variable Text", "Segoe UI", system-ui, sans-serif;
    font-size: 10pt;
}}
/* The global rule above paints every widget, which would otherwise draw the
   window background over the card surface behind labels. */
QLabel, QCheckBox {{
    background: transparent;
}}
QFrame#Sidebar {{
    background: {p.surface_alt};
    border-right: 1px solid {p.border};
}}
QLabel#Brand {{
    font-size: 15pt;
    font-weight: 600;
    padding: 18px 18px 2px 18px;
}}
QLabel#BrandSub {{
    color: {p.muted};
    font-size: 9pt;
    padding: 0 18px 16px 18px;
}}
QPushButton#NavButton {{
    background: transparent;
    border: none;
    border-radius: 6px;
    padding: 9px 14px;
    margin: 2px 10px;
    text-align: left;
    color: {p.muted};
}}
QPushButton#NavButton:hover {{
    background: {p.surface};
    color: {p.text};
}}
QPushButton#NavButton:checked {{
    background: {p.accent};
    color: {p.accent_text};
    font-weight: 600;
}}
QPushButton#NavButton:disabled {{
    color: {p.neutral};
}}
QFrame#Card {{
    background: {p.surface};
    border: 1px solid {p.border};
    border-radius: 10px;
}}
QFrame#Header {{
    background: {p.surface};
    border: 1px solid {p.border};
    border-radius: 10px;
}}
QLabel#CardTitle {{
    font-size: 11pt;
    font-weight: 600;
}}
QLabel#CardSummary {{
    color: {p.muted};
}}
QLabel#PageTitle {{
    font-size: 17pt;
    font-weight: 600;
}}
QLabel#PageSubtitle {{
    color: {p.muted};
}}
QLabel#Badge {{
    border-radius: 9px;
    padding: 2px 10px;
    font-size: 8pt;
    font-weight: 600;
}}
QPushButton {{
    background: {p.surface_alt};
    border: 1px solid {p.border};
    border-radius: 6px;
    padding: 7px 14px;
}}
QPushButton:hover {{
    border-color: {p.accent};
}}
QPushButton:disabled {{
    color: {p.neutral};
    border-color: {p.border};
}}
QPushButton#Primary {{
    background: {p.accent};
    color: {p.accent_text};
    border: 1px solid {p.accent};
    font-weight: 600;
}}
QPushButton#Primary:disabled {{
    background: {p.surface_alt};
    color: {p.neutral};
    border-color: {p.border};
}}
QPushButton#Link {{
    background: transparent;
    border: none;
    color: {p.accent};
    padding: 2px 0;
    text-align: left;
}}
QTextEdit#Detail {{
    background: {p.surface_alt};
    border: 1px solid {p.border};
    border-radius: 6px;
    font-family: "Cascadia Mono", Consolas, monospace;
    font-size: 9pt;
}}
QPlainTextEdit#Log {{
    background: {p.surface_alt};
    border: 1px solid {p.border};
    border-radius: 8px;
    font-family: "Cascadia Mono", Consolas, monospace;
    font-size: 9pt;
    color: {p.muted};
}}
QProgressBar {{
    background: {p.surface_alt};
    border: 1px solid {p.border};
    border-radius: 4px;
    height: 6px;
    text-align: center;
}}
QProgressBar::chunk {{
    background: {p.accent};
    border-radius: 3px;
}}
QTableWidget {{
    background: {p.surface};
    border: 1px solid {p.border};
    border-radius: 8px;
    gridline-color: {p.border};
}}
QTableWidget::item {{
    padding: 6px 8px;
    border-bottom: 1px solid {p.border};
}}
QTableWidget::item:selected {{
    background: {p.surface_alt};
    color: {p.text};
}}
QHeaderView::section {{
    background: {p.surface_alt};
    color: {p.muted};
    border: none;
    border-bottom: 1px solid {p.border};
    border-right: 1px solid {p.border};
    padding: 8px 6px;
    font-weight: 600;
}}
QComboBox {{
    background: {p.surface_alt};
    border: 1px solid {p.border};
    border-radius: 6px;
    padding: 6px 10px;
    min-width: 150px;
}}
QComboBox QAbstractItemView {{
    background: {p.surface};
    border: 1px solid {p.border};
    selection-background-color: {p.accent};
    selection-color: {p.accent_text};
}}
QScrollArea {{ border: none; }}
QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 2px;
}}
QScrollBar::handle:vertical {{
    background: {p.border};
    border-radius: 5px;
    min-height: 28px;
}}
QScrollBar::handle:vertical:hover {{ background: {p.neutral}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QStatusBar {{
    background: {p.surface_alt};
    border-top: 1px solid {p.border};
    color: {p.muted};
}}
"""
