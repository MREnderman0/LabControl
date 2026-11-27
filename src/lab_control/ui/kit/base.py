"""Primitives: status dot and pill, icon button, icon tile, eliding label, separators, navigation."""

from __future__ import annotations

from html import escape

from PySide6.QtCore import QPointF, QRect, QRectF, QSize, Qt, QVariantAnimation
from PySide6.QtGui import QColor, QFontMetrics, QPainter
from PySide6.QtWidgets import (
    QAbstractButton,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QWidget,
)

from lab_control.ui.kit import icons
from lab_control.ui.styles import tokens as tk


def repolish(widget: QWidget) -> None:
    """Re-apply the stylesheet after a dynamic property change."""
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    widget.update()


def set_property(widget: QWidget, name: str, value) -> None:
    """Set a style property and repolish only when it actually changed."""
    if widget.property(name) != value:
        widget.setProperty(name, value)
        repolish(widget)


def plain_tooltip(text: str) -> str:
    if "<" in text:
        return "<p>" + escape(text).replace("\n", "<br>") + "</p>"
    return text


def label(text: str = "", role: str = "", parent: QWidget | None = None) -> QLabel:
    """Text with a typographic role from the stylesheet: title, heading, caption, muted, value, mono."""
    widget = QLabel(text, parent)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    if role:
        widget.setProperty("role", role)
    return widget


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
        # Show the full text as a tooltip while elided, without replacing a tooltip set by the page.
        automatic = getattr(self, "_automatic_tip", "")
        if shown != full and self.toolTip() in ("", automatic):
            self._automatic_tip = plain_tooltip(full)
            self.setToolTip(self._automatic_tip)
        elif shown == full and automatic and self.toolTip() == automatic:
            self._automatic_tip = ""
            self.setToolTip("")
        painter.setPen(self.palette().color(self.foregroundRole()))
        painter.setFont(self.font())
        painter.drawText(rect, int(self.alignment()) | int(Qt.AlignmentFlag.AlignVCenter), shown)


class Separator(QFrame):
    """1 px rule; ``vertical`` for toolbars."""

    def __init__(self, vertical: bool = False, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("kitSeparator")
        self.setProperty("orientation", "v" if vertical else "h")
        if vertical:
            self.setFixedWidth(1)
            self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        else:
            self.setFixedHeight(1)
            self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)


class Dot(QWidget):
    """Status dot; ``live`` adds a slow pulse for running acquisition."""

    def __init__(self, status: str = "ok", size: int = 7, live: bool = False, box: int | None = None) -> None:
        super().__init__()
        self._size = size
        self._phase = 0.0
        self._animation: QVariantAnimation | None = None
        self._live = False
        self._color = QColor(tk.OK)
        self.status = ""
        extent = box or size + 6
        self.setFixedSize(extent, extent)
        self.set_status(status, live)

    def set_status(self, status: str, live: bool = False) -> None:
        self.status = status
        self._color = QColor(tk.STATUS.get(status, status))
        self._live = live
        if live and self._animation is None:
            self._animation = QVariantAnimation(self, startValue=0.0, endValue=1.0, duration=1800)
            self._animation.setLoopCount(-1)
            self._animation.valueChanged.connect(self._tick)
        self._sync_pulse()
        self.update()

    def _sync_pulse(self) -> None:
        if self._animation is None:
            return
        if self._live and self.isVisible():
            if self._animation.state() != QVariantAnimation.State.Running:
                self._animation.start()
        else:
            self._animation.stop()
            self._phase = 0.0

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._sync_pulse()

    def hideEvent(self, event) -> None:  # noqa: N802
        super().hideEvent(event)
        if self._animation is not None:
            self._animation.stop()
            self._phase = 0.0

    def _tick(self, value: float) -> None:
        self._phase = value
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        center = QPointF(self.width() / 2, self.height() / 2)
        if self._live:
            halo = QColor(self._color)
            halo.setAlphaF(0.38 * (1 - self._phase))
            radius = min(self._size / 2 + 3 * self._phase, self.width() / 2)
            painter.setBrush(halo)
            painter.drawEllipse(center, radius, radius)
        painter.setBrush(self._color)
        painter.drawEllipse(center, self._size / 2, self._size / 2)


class Pill(QFrame):
    """Rounded status label: kind is neutral, ok, warn, err or accent; optional dot or icon."""

    def __init__(self, text: str = "", kind: str = "neutral", *, dot: bool = False, icon: str = "") -> None:
        super().__init__()
        self.setObjectName("statusPill")
        self.setFixedHeight(22)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._layout = layout = QHBoxLayout(self)
        layout.setContentsMargins(7 if dot or icon else 9, 0, 9, 0)
        layout.setSpacing(5)
        self.dot = Dot(kind if kind != "neutral" else "idle", 6, box=10)
        layout.addWidget(self.dot)
        self.dot.setVisible(dot and not icon)
        self.icon_name = icon
        self.icon_view = QLabel(self)
        self.icon_view.setFixedSize(13, 13)
        layout.addWidget(self.icon_view)
        self.icon_view.setVisible(bool(icon))
        self.label = QLabel(text, self)
        self.label.setTextFormat(Qt.TextFormat.PlainText)
        self.label.setObjectName("statusPillText")
        layout.addWidget(self.label)
        self.kind = ""
        self._has_dot = dot
        self.set_kind(kind)

    def _sync_leading(self) -> None:
        show_icon = bool(self.icon_name)
        show_dot = self._has_dot and not show_icon
        self.icon_view.setVisible(show_icon)
        self.dot.setVisible(show_dot)
        self._layout.setContentsMargins(7 if (show_icon or show_dot) else 9, 0, 9, 0)
        if show_icon:
            color = tk.ACCENT_TEXT if self.kind == "accent" else tk.STATUS.get(self.kind, tk.TEXT_2)
            self.icon_view.setPixmap(icons.icon(self.icon_name, color).pixmap(QSize(13, 13)))

    def set_kind(self, kind: str, *, live: bool = False) -> None:
        self.dot.set_status(kind if kind != "neutral" else "idle", live)
        if kind != self.kind:
            self.kind = kind
            self._sync_leading()
            self.setProperty("kind", kind)
            repolish(self)
            repolish(self.label)

    def set_icon(self, icon: str = "", *, dot: bool | None = None) -> None:
        changed = icon != self.icon_name or (dot is not None and dot != self._has_dot)
        self.icon_name = icon
        if dot is not None:
            self._has_dot = dot
        if changed:
            self._sync_leading()

    def text(self) -> str:
        return self.label.text()

    def set_text(self, text: str) -> None:
        if self.label.text() != text:
            self.label.setText(text)

    def set(
        self,
        text: str,
        kind: str,
        *,
        live: bool = False,
        icon: str | None = None,
        dot: bool | None = None,
    ) -> None:
        self.set_text(text)
        if icon is not None or dot is not None:
            self.set_icon(self.icon_name if icon is None else icon, dot=dot)
        self.set_kind(kind, live=live)


class VersionChip(QAbstractButton):
    """Clickable 20 px version capsule beside the brand; ``ok`` turns it green with a trailing icon."""

    HEIGHT = 20

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("versionChip")
        self.kind = "neutral"
        self.icon_name = ""
        self.setText(text)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    def _font(self):
        font = self.font()
        font.setPixelSize(11)
        font.setWeight(font.Weight.DemiBold)
        return font

    def set_state(self, text: str, kind: str = "neutral", icon: str = "") -> None:
        if (text, kind, icon) == (self.text(), self.kind, self.icon_name):
            return
        self.setText(text)
        self.kind, self.icon_name = kind, icon
        self.updateGeometry()
        self.update()

    def sizeHint(self) -> QSize:  # noqa: N802
        width = QFontMetrics(self._font()).horizontalAdvance(self.text()) + 16
        if self.icon_name:
            width += 15
        return QSize(width, self.HEIGHT)

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return self.sizeHint()

    def enterEvent(self, event) -> None:  # noqa: N802
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        hovered = self.underMouse() or self.isDown() or self.hasFocus()
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        if self.kind == "ok":
            fill, border, ink = QColor(tk.OK), QColor(tk.OK), QColor(tk.OK)
            fill.setAlphaF(0.2 if hovered else 0.12)
            border.setAlphaF(0.55 if hovered else 0.32)
        else:
            fill = QColor(tk.HOVER if hovered else tk.SURFACE_2)
            border = QColor(tk.BORDER_HOVER if hovered else tk.BORDER)
            ink = QColor(tk.TEXT if hovered else tk.TEXT_2)
        painter.setPen(border)
        painter.setBrush(fill)
        painter.drawRoundedRect(rect, rect.height() / 2, rect.height() / 2)
        painter.setFont(self._font())
        painter.setPen(ink)
        text_rect = QRect(8, 0, self.width() - 16 - (15 if self.icon_name else 0), self.height())
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignCenter, self.text())
        if self.icon_name:
            icons.paint(painter, self.icon_name, ink.name(), QRect(self.width() - 19, 4, 12, 12))


class IconButton(QToolButton):
    """Square, borderless tool button with a line icon; optional red badge dot."""

    def __init__(
        self, icon_name: str, tooltip: str = "", size: int = 30, icon_size: int = 16, *, badge: bool = False
    ) -> None:
        super().__init__()
        self.setObjectName("iconButton")
        self.icon_name = icon_name
        self.badge = badge
        self.setIcon(icons.icon(icon_name, tk.TEXT_2, checked=tk.ACCENT))
        self.setIconSize(QSize(icon_size, icon_size))
        self.setFixedSize(size, size)
        self.setToolTip(tooltip)
        self.setAccessibleName(tooltip)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_badge(self, badge: bool) -> None:
        if badge != self.badge:
            self.badge = badge
            self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)
        if self.badge:
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(QColor(tk.BG))
            painter.setBrush(QColor(tk.ERR))
            painter.drawEllipse(QPointF(self.width() - 9, 8), 4, 4)


class IconTile(QWidget):
    """Rounded accent-tinted square with a category icon."""

    def __init__(self, icon_name: str, size: int = 34, color: str = tk.ACCENT) -> None:
        super().__init__()
        self.icon_name = icon_name
        self.color = color
        self.setFixedSize(size, size)

    def set_icon(self, icon_name: str, color: str | None = None) -> None:
        self.icon_name = icon_name
        self.color = color or self.color
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        background = QColor(self.color)
        background.setAlphaF(0.13)
        painter.setBrush(background)
        radius = 9 if self.width() >= 30 else 7
        painter.drawRoundedRect(QRectF(self.rect()), radius, radius)
        inner = round(self.width() * 0.52)
        offset = (self.width() - inner) / 2
        icons.paint(painter, self.icon_name, self.color, QRectF(offset, offset, inner, inner))


class NavButton(QPushButton):
    """Sidebar entry. Keeps the QPushButton API; paints icon, label and optional count."""

    def __init__(self, label: str, icon_name: str, parent: QWidget | None = None) -> None:
        super().__init__(label, parent)
        self.setObjectName("workspaceNav")
        self.icon_name = icon_name
        self.badge = 0
        self.collapsed = False
        self.parent_active = False
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

    def set_parent_active(self, active: bool) -> None:
        """A child entry is selected: tint the icon without the selected background."""
        if active != self.parent_active:
            self.parent_active = active
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
        lit = checked or self.parent_active
        icon_color = tk.ACCENT if lit else tk.TEXT if hovered else tk.TEXT_2
        if self.collapsed:
            icons.paint(
                painter, self.icon_name, icon_color, QRect((self.width() - 19) // 2, (h - 19) // 2, 19, 19)
            )
        else:
            icons.paint(painter, self.icon_name, icon_color, QRect(11, (h - 17) // 2, 17, 17))
            font = self.font()
            font.setBold(checked)
            painter.setFont(font)
            painter.setPen(QColor(tk.TEXT if lit or hovered else tk.TEXT_2))
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


class SubNavButton(QPushButton):
    """Child entry under a sidebar group (tree line, accent bar when selected, right-aligned count)."""

    def __init__(self, label: str, count: int = 0, parent: QWidget | None = None) -> None:
        super().__init__(label, parent)
        self.setObjectName("workspaceSubNav")
        self.count = count
        self.alert = False
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self.setFixedHeight(30)
        self.setAccessibleName(label)

    def set_count(self, count: int, alert: bool = False) -> None:
        if (count, alert) != (self.count, self.alert):
            self.count, self.alert = count, alert
            self.update()

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(180, 30)

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return QSize(40, 30)

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
        h = self.height()
        if checked or hovered:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(tk.SURFACE_2 if checked else tk.HOVER))
            painter.drawRoundedRect(QRectF(self.rect()).adjusted(28, 0, 0, 0), tk.RADIUS_SM, tk.RADIUS_SM)
        painter.setPen(QColor(tk.BORDER_STRONG))
        painter.drawLine(19, 0, 19, h)
        if checked:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(tk.ACCENT))
            painter.drawRoundedRect(QRectF(18, 8, 3, h - 16), 1.5, 1.5)
        font = self.font()
        font.setPixelSize(12)
        font.setBold(checked)
        painter.setFont(font)
        painter.setPen(QColor(tk.TEXT if checked or hovered else tk.TEXT_2))
        painter.drawText(QRect(40, 0, self.width() - 76, h), Qt.AlignmentFlag.AlignVCenter, self.text())
        if self.count:
            font.setBold(False)
            font.setPixelSize(11)
            painter.setFont(font)
            painter.setPen(QColor(tk.ERR if self.alert else tk.TEXT_3))
            painter.drawText(
                QRect(0, 0, self.width() - 10, h),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                str(self.count),
            )
