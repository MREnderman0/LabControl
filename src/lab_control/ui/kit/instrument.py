"""Instrument page pieces: device card painter, quick-switch chips and the page header row."""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import QPointF, QRect, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter
from PySide6.QtWidgets import QHBoxLayout, QSizePolicy, QWidget

from lab_control.ui.kit import icons
from lab_control.ui.kit.controls import Segmented
from lab_control.ui.styles import tokens as tk


def paint_device_card(
    painter: QPainter,
    rect: QRect,
    *,
    icon_name: str,
    name: str,
    subtitle: str,
    state: str = "",
    kind: str = "neutral",
    font: QFont,
    hovered: bool = False,
    open_: bool = False,
    enabled: bool = True,
) -> None:
    """Device card: icon tile, model with a status pill, vendor · serial · slot, up/down chevron."""
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    frame = QRectF(rect).adjusted(0.5, 0.5, -0.5, -0.5)
    painter.setPen(QColor(tk.ACCENT if open_ else tk.BORDER_STRONG if hovered else tk.BORDER))
    painter.setBrush(QColor("#18191d" if hovered else tk.SURFACE))
    painter.drawRoundedRect(frame, tk.RADIUS, tk.RADIUS)
    if not enabled:
        painter.setOpacity(0.55)
    tile = QRectF(rect.left() + 8, rect.top() + (rect.height() - 38) / 2, 38, 38)
    tint = QColor(tk.ACCENT)
    tint.setAlphaF(0.13)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(tint)
    painter.drawRoundedRect(tile, 9, 9)
    icons.paint(painter, icon_name, tk.ACCENT, tile.adjusted(9, 9, -9, -9))
    left = tile.right() + 12
    right = rect.right() - 34
    title_font = QFont(font)
    title_font.setPixelSize(15)
    title_font.setWeight(QFont.Weight.DemiBold)
    small = QFont(font)
    small.setPixelSize(12)
    pill_font = QFont(font)
    pill_font.setPixelSize(12)
    pill_font.setWeight(QFont.Weight.Medium)
    top = rect.top() + rect.height() / 2 - 20
    pill_width = QFontMetrics(pill_font).horizontalAdvance(state) + 26 if state else 0
    title_metrics = QFontMetrics(title_font)
    room = max(0.0, right - left - (pill_width + 8 if state else 0))
    shown = title_metrics.elidedText(name, Qt.TextElideMode.ElideRight, int(room))
    painter.setFont(title_font)
    painter.setPen(QColor(tk.TEXT if state or name else tk.TEXT_2))
    painter.drawText(QRectF(left, top, room, 22), int(Qt.AlignmentFlag.AlignVCenter), shown)
    if state:
        x = left + title_metrics.horizontalAdvance(shown) + 8
        pill = QRectF(x, top + 1, pill_width, 20)
        color = QColor(tk.ACCENT_TEXT if kind == "accent" else tk.STATUS.get(kind, tk.TEXT_2))
        background = QColor(color)
        background.setAlphaF(0.13 if kind != "neutral" else 0.0)
        painter.setPen(QColor(tk.BORDER) if kind == "neutral" else Qt.PenStyle.NoPen)
        painter.setBrush(background if kind != "neutral" else QColor(tk.SURFACE_2))
        painter.drawRoundedRect(pill, 10, 10)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(tk.STATUS.get(kind, tk.IDLE)) if kind != "neutral" else QColor(tk.IDLE))
        painter.drawEllipse(QPointF(pill.left() + 10, pill.center().y()), 3, 3)
        painter.setFont(pill_font)
        painter.setPen(color)
        painter.drawText(pill.adjusted(17, 0, -8, 0), int(Qt.AlignmentFlag.AlignVCenter), state)
    painter.setFont(small)
    painter.setPen(QColor(tk.TEXT_3))
    meta = QFontMetrics(small).elidedText(subtitle, Qt.TextElideMode.ElideRight, int(right - left))
    painter.drawText(QRectF(left, top + 23, right - left, 18), int(Qt.AlignmentFlag.AlignVCenter), meta)
    painter.setOpacity(1.0)
    icons.paint(
        painter,
        "updown",
        tk.ACCENT if open_ else tk.TEXT_3,
        QRectF(rect.right() - 26, rect.center().y() - 8, 16, 16),
    )
    painter.restore()


class DeviceChips(Segmented):
    """One-click chips with a status dot and a short label.

    Used for same-category devices, and for the roles of a multi-function instrument that runs
    several roles at once (Moku:Pro).
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__((), parent)
        self.setObjectName("deviceChips")
        # Chips never set the page's minimum width; the page hides them when space runs out.
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.signature: tuple = ()

    def set_devices(self, devices: Sequence[tuple[str, str, str, str]], current: str) -> None:
        """``devices``: (short label, device id, status kind, tooltip)."""
        signature = tuple(devices)
        if signature != self.signature and [d[1] for d in devices] == self._data:
            # Same items, new state: update in place. Rebuilding would blank the chips until the
            # new buttons are shown, which flickers on every status change.
            self.signature = signature
            for index, (text, _device_id, kind, tooltip) in enumerate(devices):
                chip = self.group.button(index)
                if chip.text() != text:
                    chip.setText(text)
                chip.setIcon(icons.icon("record", tk.STATUS.get(kind, tk.IDLE)))
                chip.setToolTip(tooltip)
            # sizeHint comes from the row; tell the parent layout that the chip widths changed.
            self.updateGeometry()
        elif signature != self.signature:
            self.signature = signature
            for item in list(self.group.buttons()):
                self.group.removeButton(item)
                self.row.removeWidget(item)
                item.hide()
                item.deleteLater()
            self._data = []
            for text, device_id, kind, tooltip in devices:
                chip = self.add_item(text, device_id, icon="record", icon_color=tk.STATUS.get(kind, tk.IDLE))
                chip.setToolTip(tooltip)
        self.set_current(current)

    def sizeHint(self):  # noqa: N802
        return self.row.sizeHint()


class PageHeader(QWidget):
    """Top row of an instrument page: device switch and chips left, run controls right."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("pageHeader")
        self.row = QHBoxLayout(self)
        self.row.setContentsMargins(0, 0, 0, 0)
        self.row.setSpacing(8)
        self.left = QHBoxLayout()
        self.left.setSpacing(10)
        self.right = QHBoxLayout()
        self.right.setSpacing(8)
        self.row.addLayout(self.left, 1)
        self.row.addLayout(self.right)

    def add_left(self, widget: QWidget, stretch: int = 0) -> None:
        self.left.addWidget(widget, stretch)

    def add_right(self, widget: QWidget) -> None:
        self.right.addWidget(widget)
