"""Line icons of the component kit (24×24 grid, 1.8 stroke) rendered from inline SVG.

Icons are rasterised at several sizes so they stay sharp at 100–200 % scaling,
without writing files at runtime. Checked buttons use the ``On`` state color.
"""

from __future__ import annotations

from functools import lru_cache

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

from lab_control.ui.styles import tokens as tk

STROKE = {
    "home": '<path d="M3 11l9-7 9 7v9a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z"/>',
    "sliders": '<path d="M4 6h9M19 6h1M4 12h3M13 12h7M4 18h11M20 18h0"/><circle cx="16" cy="6" r="2.4"/>'
    '<circle cx="10" cy="12" r="2.4"/><circle cx="17.5" cy="18" r="2.4"/>',
    "eye": '<path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
    "chart": '<path d="M3 3v18h18"/><path d="M7 15l4-5 3 3 5-7"/>',
    "log": '<path d="M6 3h9l4 4v14H6z"/><path d="M14 3v5h5M9 13h7M9 17h5"/>',
    "gear": '<circle cx="12" cy="12" r="3.2"/><path d="M12 2.5v3M12 18.5v3M2.5 12h3M18.5 12h3M5.3 5.3l2.1 2.1'
    'M16.6 16.6l2.1 2.1M5.3 18.7l2.1-2.1M16.6 7.4l2.1-2.1"/>',
    "scan": '<path d="M21 12a9 9 0 1 1-2.6-6.4"/><path d="M21 4v5h-5"/>',
    "search": '<circle cx="11" cy="11" r="7"/><path d="M20 20l-4-4"/>',
    "plug": '<path d="M9 2v6M15 2v6M6 8h12v3a6 6 0 0 1-12 0zM12 17v5"/>',
    "list": '<path d="M8 6h13M8 12h13M8 18h13"/><circle cx="4" cy="6" r="1"/><circle cx="4" cy="12" r="1"/>'
    '<circle cx="4" cy="18" r="1"/>',
    "stethoscope": '<path d="M5 3v6a5 5 0 0 0 10 0V3M10 14v2a5 5 0 0 0 10 0v-2"/><circle cx="20" cy="12" r="2"/>',
    "arrow": '<path d="M5 12h14M13 6l6 6-6 6"/>',
    "x": '<path d="M6 6l12 12M18 6L6 18"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "minus": '<path d="M5 12h14"/>',
    "alert": '<path d="M12 3l10 18H2z"/><path d="M12 10v5M12 18v.5"/>',
    "download": '<path d="M12 4v11M7 10l5 5 5-5M5 20h14"/>',
    "expand": '<path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/>',
    "tools": '<path d="M14.7 6.3a4 4 0 0 0-5.4 5.4L3 18l3 3 6.3-6.3a4 4 0 0 0 5.4-5.4l-2.5 2.5-2.5-2.5z"/>',
    "lock": '<rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/>',
    "zap": '<path d="M13 2L4 14h7l-1 8 9-12h-7z"/>',
    "scope": '<rect x="3" y="4" width="18" height="16" rx="2.5"/><path d="M6 14l3-5 3 6 3-8 3 7"/>',
    "spec": '<path d="M3 20h18M5 20v-3M8 20v-6M11 20V5M14 20v-8M17 20v-4M20 20v-2"/>',
    "target": '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1.2"/>',
    "sine": '<path d="M2 12c2.5-7 5-7 7.5 0s5 7 7.5 0c1.2-3.4 2.6-4.6 5-3"/>',
    "thermo": '<path d="M14 14.8V5a2 2 0 0 0-4 0v9.8a4 4 0 1 0 4 0z"/>',
    "meter": '<rect x="5" y="2.5" width="14" height="19" rx="2.5"/><rect x="8" y="5.5" width="8" height="5" rx="1"/>'
    '<circle cx="12" cy="16" r="2.2"/>',
    "clock": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    "network": '<path d="M3 12h4l3-8 4 16 3-8h4"/>',
    "noise": '<path d="M2 12h2l1-4 2 9 2-12 2 14 2-10 2 6 2-3 1 2h4"/>',
    "load": '<rect x="3" y="6" width="18" height="12" rx="2"/><path d="M7 10v4M11 10v4M15 10v4"/>',
    "pump": '<circle cx="12" cy="12" r="8"/><path d="M12 4v8l5 3"/>',
    "switch": '<rect x="2" y="7" width="20" height="10" rx="5"/><circle cx="16" cy="12" r="3"/>',
    "prism": '<path d="M12 3l9 16H3z"/><path d="M2 10h7M15 13l7 2M15 15l7 5"/>',
    "box": '<path d="M3 7l9-4 9 4v10l-9 4-9-4z"/><path d="M3 7l9 4 9-4M12 11v10"/>',
    "bell": '<path d="M6 16v-5a6 6 0 0 1 12 0v5l2 2H4z"/><path d="M10 21h4"/>',
    "eyeoff": '<path d="M3 3l18 18"/><path d="M10.6 5.1A10 10 0 0 1 12 5c6.4 0 10 7 10 7a17 17 0 0 1-3.1 3.9'
    'M6.6 6.6C3.7 8.4 2 12 2 12s3.6 7 10 7a9.6 9.6 0 0 0 5.4-1.6"/><path d="M9.9 9.9a3 3 0 0 0 4.2 4.2"/>',
    "rise": '<path d="M3 18h6V6h12"/>',
    "fall": '<path d="M3 6h6v12h12"/>',
    "wand": '<path d="M15 4V2M15 10V8M11 6h2M17 6h2M4 20l10-10"/><path d="M19 13v2M18 14h2"/>',
    "down": '<path d="M6 9l6 6 6-6"/>',
    "up": '<path d="M6 15l6-6 6 6"/>',
    "right": '<path d="M9 6l6 6-6 6"/>',
    "updown": '<path d="M8 9l4-4 4 4M8 15l4 4 4-4"/>',
    "cross": '<path d="M3 12h4M17 12h4M12 3v4M12 17v4"/><circle cx="12" cy="12" r="4"/>',
    "check": '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
    "inbox": '<path d="M3 13l3-8h12l3 8v6H3z"/><path d="M3 13h5l1.5 2.5h5L16 13h5"/>',
    "folder": '<path d="M3 6.5A1.5 1.5 0 0 1 4.5 5H9l2 2.5h8.5A1.5 1.5 0 0 1 21 9v9.5a1.5 1.5 0 0 1-1.5 1.5h-15A1.5 1.5 0 0 1 3 18.5z"/>',
    "refresh": '<path d="M20 11a8 8 0 0 0-14.3-4.9L4 8M4 4v4h4M4 13a8 8 0 0 0 14.3 4.9L20 16M20 20v-4h-4"/>',
    "undo": '<path d="M9 14L4 9l5-5"/><path d="M4 9h10.5a5.5 5.5 0 0 1 0 11H11"/>',
    "filter": '<path d="M3 5h18l-7 8v6l-4 2v-8z"/>',
    "more": '<circle cx="5" cy="12" r="1.2"/><circle cx="12" cy="12" r="1.2"/><circle cx="19" cy="12" r="1.2"/>',
    "pin": '<path d="M9 4h6l-1 6 3 3H7l3-3zM12 13v8"/>',
    "window": '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 9h18"/>',
    "save": '<path d="M5 3h11l3 3v13a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2z"/><path d="M8 3v5h7V3M7 21v-7h10v7"/>',
    "power": '<path d="M12 3v9"/><path d="M6.3 6.8a8 8 0 1 0 11.4 0"/>',
    "info": '<circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8v.5"/>',
    "upgrade": '<circle cx="12" cy="12" r="9"/><path d="M12 16.5v-9M8 11.5l4-4 4 4"/>',
    "channels": '<path d="M4 7h16M4 12h16M4 17h16"/>',
    "single": '<path d="M4 12h3l2-6 3 12 2-6h6"/>',
}
FILL = {
    "pause": '<rect x="6" y="5" width="4" height="14" rx="1"/><rect x="14" y="5" width="4" height="14" rx="1"/>',
    "stop": '<rect x="6" y="6" width="12" height="12" rx="2"/>',
    "play": '<path d="M7 5v14l12-7z"/>',
    "record": '<circle cx="12" cy="12" r="6"/>',
}

CATEGORY_ICONS = {
    "accelerometer": "sine",
    "measurement_analyzers": "meter",
    "load_drivers": "load",
    "signal_generator": "sine",
    "power_supply": "zap",
    "oscilloscope": "scope",
    "spectrum_analyzer": "spec",
    "optical_power_meter": "target",
    "multimeter": "meter",
    "frequency_counter": "clock",
    "network_analyzer": "network",
    "phase_noise_analyzer": "noise",
    "temperature_controller": "thermo",
    "electronic_load": "load",
    "pump_driver": "pump",
    "output_switch": "switch",
    "optical_spectrometer": "prism",
}

_SIZES = (14, 16, 18, 20, 24, 28, 32, 40, 48, 64)


def svg(name: str, color: str, stroke_width: float = 1.8) -> bytes:
    if name in FILL:
        attrs, body = f'fill="{color}" stroke="none"', FILL[name]
    else:
        attrs = (
            f'fill="none" stroke="{color}" stroke-width="{stroke_width}" '
            'stroke-linecap="round" stroke-linejoin="round"'
        )
        body = STROKE.get(name, STROKE["box"])
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" {attrs}>{body}</svg>'
    ).encode()


@lru_cache(maxsize=256)
def _renderer(data: bytes) -> QSvgRenderer:
    return QSvgRenderer(QByteArray(data))


def _pixmap(data: bytes, size: int) -> QPixmap:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    _renderer(data).render(painter, QRectF(0, 0, size, size))
    painter.end()
    return pixmap


@lru_cache(maxsize=None)
def icon(
    name: str, color: str = tk.TEXT_2, checked: str | None = None, disabled: str = tk.TEXT_DISABLED
) -> QIcon:
    """Icon in ``color``; ``checked`` colors the On state (checked buttons)."""
    result = QIcon()
    variants = [(color, QIcon.Mode.Normal, QIcon.State.Off), (disabled, QIcon.Mode.Disabled, QIcon.State.Off)]
    if checked:
        variants += [
            (checked, QIcon.Mode.Normal, QIcon.State.On),
            (disabled, QIcon.Mode.Disabled, QIcon.State.On),
        ]
    for tint, mode, state in variants:
        data = svg(name, tint)
        for size in _SIZES:
            result.addPixmap(_pixmap(data, size), mode, state)
    return result


def paint(painter: QPainter, name: str, color: str, rect) -> None:
    """Draw an icon directly (for custom-painted widgets)."""
    _renderer(svg(name, color)).render(painter, QRectF(rect))
