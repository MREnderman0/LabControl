"""Shared graphite components: navigation button, status dot/pill, icon button, eliding label."""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRect, QRectF, QSize, Qt, QVariantAnimation
from PySide6.QtGui import QColor, QFontMetrics, QPainter
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QToolButton, QWidget

from lab_control.ui.styles import tokens as tk
from lab_control.ui.widgets import icons


def repolish(widget: QWidget) -> None:
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    widget.update()


class NavButton(QPushButton):
    """Sidebar entry. Keeps the QPushButton API; paints icon, label and optional count."""

    def __init__(self, label: str, icon_name: str, parent: QWidget | None = None) -> None:
        super().__init__(label, parent)
        self.setObjectName("workspaceNav")
        self.icon_name = icon_name
        self.badge = 0
        self.collapsed = False
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self.setMinimumHeight(36)
        self.setAccessibleName(label)
        self.setToolTip(label)

    def set_badge(self, count: int) -> None:
        if count != self.badge:
            self.badge = count
            self.update()

    def set_collapsed(self, collapsed: bool) -> None:
        self.collapsed = collapsed
        self.update()

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(180, 36)

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return QSize(40, 36)

    def enterEvent(self, event) -> None:  # noqa: N802
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        checked, hovered = self.isChecked(), self.underMouse() and self.isEnabled()
        if checked or hovered:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(tk.SURFACE_2 if checked else tk.HOVER))
            painter.drawRoundedRect(QRectF(self.rect()), tk.RADIUS_SM, tk.RADIUS_SM)
        h = self.height()
        icon_color = tk.ACCENT if checked else tk.TEXT if hovered else tk.TEXT_2
        if self.collapsed:
            icons.paint(
                painter, self.icon_name, icon_color, QRect((self.width() - 19) // 2, (h - 19) // 2, 19, 19)
            )
        else:
            icons.paint(painter, self.icon_name, icon_color, QRect(11, (h - 17) // 2, 17, 17))
            font = self.font()
            font.setBold(checked)
            painter.setFont(font)
            painter.setPen(QColor(tk.TEXT if checked or hovered else tk.TEXT_2))
            painter.drawText(QRect(38, 0, self.width() - 80, h), Qt.AlignmentFlag.AlignVCenter, self.text())
        if self.badge:
            font = self.font()
            font.setPixelSize(11)
            font.setBold(True)
            text = str(self.badge) if self.badge < 100 else "99+"
            w = max(18, QFontMetrics(font).horizontalAdvance(text) + 10)
            if self.collapsed:
                rect = QRectF(self.width() - w - 2, 2, w, 16)
            else:
                rect = QRectF(self.width() - w - 8, (h - 18) / 2, w, 18)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(tk.ERR))
            painter.drawRoundedRect(rect, rect.height() / 2, rect.height() / 2)
            painter.setPen(QColor("#ffffff"))
            painter.setFont(font)
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)


class Dot(QWidget):
    """Status dot; ``live`` adds a slow pulse for running acquisition."""

    def __init__(self, status: str = "ok", size: int = 7, live: bool = False, box: int | None = None) -> None:
        super().__init__()
        self._size = size
        self._phase = 0.0
        self._animation: QVariantAnimation | None = None
        self._color = QColor(tk.OK)
        extent = box or size + 6
        self.setFixedSize(extent, extent)
        self.set_status(status, live)

    def set_status(self, status: str, live: bool = False) -> None:
        self._color = QColor(tk.STATUS.get(status, status))
        if live and self._animation is None:
            self._animation = QVariantAnimation(self, startValue=0.0, endValue=1.0, duration=1800)
            self._animation.setLoopCount(-1)
            self._animation.valueChanged.connect(self._tick)
            self._animation.start()
        elif not live and self._animation is not None:
            self._animation.stop()
            self._animation = None
            self._phase = 0.0
        self.update()

    def _tick(self, value: float) -> None:
        self._phase = value
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        center = QPointF(self.width() / 2, self.height() / 2)
        if self._animation is not None:
            halo = QColor(self._color)
            halo.setAlphaF(0.38 * (1 - self._phase))
            radius = min(self._size / 2 + 3 * self._phase, self.width() / 2)
            painter.setBrush(halo)
            painter.drawEllipse(center, radius, radius)
        painter.setBrush(self._color)
        painter.drawEllipse(center, self._size / 2, self._size / 2)


class Pill(QFrame):
    """Rounded status label: kind is neutral, ok, warn, err or accent."""

    def __init__(self, text: str = "", kind: str = "neutral", *, dot: bool = False) -> None:
        super().__init__()
        self.setObjectName("statusPill")
        self.setFixedHeight(22)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(7 if dot else 9, 0, 9, 0)
        layout.setSpacing(5)
        self.dot = Dot(kind if kind != "neutral" else "idle", 6, box=10) if dot else None
        if self.dot:
            layout.addWidget(self.dot)
        self.label = QLabel(text)
        self.label.setObjectName("statusPillText")
        layout.addWidget(self.label)
        self.kind = ""
        self.set_kind(kind)

    def set_kind(self, kind: str, *, live: bool = False) -> None:
        if self.dot:
            self.dot.set_status(kind if kind != "neutral" else "idle", live)
        if kind != self.kind:
            self.kind = kind
            self.setProperty("kind", kind)
            repolish(self)
            repolish(self.label)

    def set_text(self, text: str) -> None:
        self.label.setText(text)


class IconButton(QToolButton):
    """Square, borderless tool button with a line icon."""

    def __init__(self, icon_name: str, tooltip: str = "", size: int = 30, icon_size: int = 16) -> None:
        super().__init__()
        self.setObjectName("iconButton")
        self.setIcon(icons.icon(icon_name, tk.TEXT_2))
        self.setIconSize(QSize(icon_size, icon_size))
        self.setFixedSize(size, size)
        self.setToolTip(tooltip)
        self.setAccessibleName(tooltip)
        self.setCursor(Qt.CursorShape.PointingHandCursor)


class ElideLabel(QLabel):
    """Single-line label that elides instead of widening its parent; full text in the tooltip."""

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        hint = super().minimumSizeHint()
        return QSize(min(hint.width(), 36), hint.height())

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        rect = self.contentsRect()
        full = self.text()
        shown = self.fontMetrics().elidedText(full, Qt.TextElideMode.ElideRight, rect.width())
        self.setToolTip(full if shown != full else "")
        painter.setPen(self.palette().color(self.foregroundRole()))
        painter.setFont(self.font())
        painter.drawText(rect, int(self.alignment()) | int(Qt.AlignmentFlag.AlignVCenter), shown)
