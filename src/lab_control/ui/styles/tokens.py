"""Graphite design tokens shared by the stylesheet, painted widgets and plots.

The runtime theme (``card_workspace.qss``) uses these literal values. Painted
widgets and pyqtgraph views import them instead of repeating hex colors, so a
palette change stays in one place.
"""

from __future__ import annotations

# Surfaces, darkest first.
BG = "#0f1012"
SIDE = "#0b0c0e"
SURFACE = "#16171a"
SURFACE_HOVER = "#18191d"
SURFACE_2 = "#1c1d21"
HOVER = "#222428"
PRESSED = "#2a2c31"
INPUT = "#121315"
CHART_BG = "#131416"
POPUP = "#1c1d21"
SCRIM = "#060608"  # Backdrop behind modal sheets, drawn with partial alpha.

# Lines.
BORDER = "#25272c"
BORDER_STRONG = "#34363d"
BORDER_HOVER = "#43464e"
GRID = "#1f2125"

# Text.
TEXT = "#ececee"
TEXT_2 = "#a3a5ac"
TEXT_3 = "#6e7179"
TEXT_DISABLED = "#4f525a"

# Brand accent: primary actions, selection and the main trace only.
ACCENT = "#ee9a48"
ACCENT_HOVER = "#f4ab62"
ACCENT_INK = "#1b1207"
ACCENT_SOFT = "#2b2118"  # ACCENT at ~13% over SURFACE
ACCENT_TEXT = "#f3b97c"

# Status colors stay separate from the brand accent.
OK = "#3fbf6f"
WARN = "#e2c34a"
ERR = "#f2555a"
INFO = "#6ab0ff"
IDLE = "#5b5e66"
STATUS = {"ok": OK, "warn": WARN, "err": ERR, "error": ERR, "info": INFO, "idle": IDLE, "accent": ACCENT}

# Channel/trace colors: identical in plots, legends, labels and control panels.
CHANNEL_COLORS = ("#f2b44c", "#4fc3e8", "#8fd16a", "#c49cf5", "#6ab0ff", "#f27fa5", "#5fd4d0", "#e2c34a")

RADIUS = 10
RADIUS_SM = 7

FONT_UI = ("Segoe UI Variable Text", "Segoe UI Variable", "Segoe UI", "Microsoft YaHei UI", "Microsoft YaHei")
FONT_MONO = ("Cascadia Mono", "Consolas", "Microsoft YaHei UI")


def channel_color(channel: int) -> str:
    return CHANNEL_COLORS[(channel - 1) % len(CHANNEL_COLORS)]


def rgba(hex_color: str, alpha: float) -> str:
    """CSS rgba() for QSS; alpha from 0 to 1."""
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"rgba({r}, {g}, {b}, {round(alpha * 255)})"
