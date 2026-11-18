"""Inputs and buttons: button variants, switch, segmented control, field box, search box."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from PySide6.QtCore import QEvent, QObject, QPointF, QRectF, QSize, Qt, QVariantAnimation, Signal
from PySide6.QtGui import QColor, QKeySequence, QPainter, QShortcut
from PySide6.QtWidgets import (
    QAbstractButton,
    QButtonGroup,
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QWidget,
)

from lab_control.ui.kit import icons
from lab_control.ui.kit.base import repolish, set_property
from lab_control.ui.styles import tokens as tk

ICON_COLORS = {"primary": tk.ACCENT_INK, "danger": tk.ERR, "ghost": tk.TEXT_2, "link": tk.ACCENT}


def style_button(
    button: QAbstractButton, variant: str = "", icon: str = "", size: str = "", icon_color: str = ""
) -> QAbstractButton:
    """Apply a kit look to an existing button without touching its signals.

    ``variant``: "" (secondary), primary, danger, ghost, link. ``size``: "" (32 px), sm (26 px), lg (36 px).
    """
    set_property(button, "variant", variant or None)
    set_property(button, "kitSize", size or None)  # Not "size": that is QWidget.size.
    if icon:
        color = icon_color or ICON_COLORS.get(variant, tk.TEXT)
        # Pages call this on every refresh; only touch the icon when it changes, because a new
        # icon invalidates the button geometry until the layout runs again.
        if button.property("kitIcon") != (icon, color, size):
            button.setProperty("kitIcon", (icon, color, size))
            button.setIcon(icons.icon(icon, color, disabled=tk.TEXT_DISABLED))
            button.setIconSize(QSize(14, 14) if size == "sm" else QSize(15, 15))
    if button.cursor().shape() != Qt.CursorShape.PointingHandCursor:
        button.setCursor(Qt.CursorShape.PointingHandCursor)
    return button


def button(
    text: str, variant: str = "", icon: str = "", size: str = "", parent: QWidget | None = None
) -> QPushButton:
    """New push button in one of the kit variants."""
    widget = QPushButton(text, parent)
    style_button(widget, variant, icon, size)
    return widget


class Switch(QCheckBox):
    """Toggle switch. A QCheckBox, so ``isChecked``/``toggled``/``stateChanged`` keep working."""

    TRACK = QSize(30, 17)

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setObjectName("kitSwitch")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._position = 0.0
        self._animation = QVariantAnimation(self, duration=140)
        self._animation.valueChanged.connect(self._move)
        self.toggled.connect(self._animate)
        self._position = 1.0 if self.isChecked() else 0.0

    def _animate(self, on: bool) -> None:
        self._animation.stop()
        if not self.isVisible():
            self._position = 1.0 if on else 0.0
            self.update()
            return
        self._animation.setStartValue(self._position)
        self._animation.setEndValue(1.0 if on else 0.0)
        self._animation.start()

    def _move(self, value: float) -> None:
        self._position = value
        self.update()

    def setChecked(self, checked: bool) -> None:  # noqa: N802
        super().setChecked(checked)
        if not self.isVisible():
            self._position = 1.0 if checked else 0.0

    def sizeHint(self) -> QSize:  # noqa: N802
        text = self.text()
        width = self.TRACK.width() + (8 + self.fontMetrics().horizontalAdvance(text) if text else 0)
        return QSize(width, max(self.TRACK.height() + 2, self.fontMetrics().height() + 2))

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return self.sizeHint()

    def hitButton(self, pos) -> bool:  # noqa: N802
        return self.rect().contains(pos)

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.isEnabled():
            painter.setOpacity(0.45)
        track = QRectF(
            0.5,
            (self.height() - self.TRACK.height()) / 2 + 0.5,
            self.TRACK.width() - 1,
            self.TRACK.height() - 1,
        )
        on = self.isChecked()
        painter.setPen(QColor(tk.ACCENT if on else tk.BORDER_STRONG))
        painter.setBrush(QColor(tk.ACCENT if on else tk.INPUT))
        painter.drawRoundedRect(track, track.height() / 2, track.height() / 2)
        radius = 5.5
        x = track.left() + 2 + radius + self._position * (track.width() - 4 - 2 * radius)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(tk.ACCENT_INK if on else tk.TEXT_3))
        painter.drawEllipse(QPointF(x, track.center().y()), radius, radius)
        if self.text():
            painter.setOpacity(1.0 if self.isEnabled() else 0.6)
            painter.setPen(self.palette().color(self.foregroundRole()))
            painter.setFont(self.font())
            painter.drawText(
                QRectF(self.TRACK.width() + 8, 0, self.width() - self.TRACK.width() - 8, self.height()),
                int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                self.text(),
            )


class Segmented(QFrame):
    """Segmented control (mutually exclusive). Items carry data; ``changed`` emits the data."""

    changed = Signal(object)

    def __init__(
        self, items: Iterable[tuple[str, Any]] = (), parent: QWidget | None = None, *, small=False
    ) -> None:
        super().__init__(parent)
        self.setObjectName("segmented")
        self.setProperty("small", small)
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        self.row = QHBoxLayout(self)
        self.row.setContentsMargins(3, 3, 3, 3)
        self.row.setSpacing(2)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.group.idClicked.connect(lambda index: self.changed.emit(self.data(index)))
        self._data: list[Any] = []
        self.set_items(items)

    def set_items(self, items: Iterable[tuple[str, Any]], current: Any = None) -> None:
        for item in list(self.group.buttons()):
            self.group.removeButton(item)
            self.row.removeWidget(item)
            item.hide()
            item.deleteLater()
        self._data = []
        for index, (text, data) in enumerate(items):
            self.add_item(text, data, index=index)
        if current is not None:
            self.set_current(current)
        elif self.group.buttons():
            self.group.buttons()[0].setChecked(True)

    def add_item(
        self, text: str, data: Any = None, *, index: int | None = None, icon: str = "", icon_color: str = ""
    ) -> QPushButton:
        item = QPushButton(text, self)
        item.setProperty("segItem", True)
        item.setCheckable(True)
        item.setCursor(Qt.CursorShape.PointingHandCursor)
        if icon:
            item.setIcon(icons.icon(icon, icon_color or tk.TEXT_2))
            item.setIconSize(QSize(9, 9) if icon == "record" else QSize(14, 14))
        index = len(self._data) if index is None else index
        self._data.append(data)
        self.group.addButton(item, index)
        self.row.addWidget(item)
        return item

    def data(self, index: int) -> Any:
        return self._data[index] if 0 <= index < len(self._data) else None

    def current(self) -> Any:
        return self.data(self.group.checkedId())

    def set_current(self, data: Any) -> None:
        if data in self._data:
            button = self.group.button(self._data.index(data))
            if button is not None and not button.isChecked():
                button.setChecked(True)

    def button(self, index: int) -> QAbstractButton | None:
        return self.group.button(index)

    def count(self) -> int:
        return len(self._data)


class FieldBox(QFrame):
    """One bordered input: value editor plus optional unit selector, focus and state rings.

    States: ``focus`` (accent border), ``dirty`` (soft accent), ``error`` (red). The children keep
    their own APIs; the box only provides the shared frame.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("fieldBox")
        self.row = QHBoxLayout(self)
        self.row.setContentsMargins(0, 0, 0, 0)
        self.row.setSpacing(0)
        self._watched: list[QWidget] = []

    def add(self, widget: QWidget, stretch: int = 0) -> None:
        self.row.addWidget(widget, stretch)
        widget.installEventFilter(self)
        self._watched.append(widget)

    def set_state(self, name: str, on: bool) -> None:
        set_property(self, name, bool(on))

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        if event.type() in (QEvent.Type.FocusIn, QEvent.Type.FocusOut):
            self.set_state("focus", any(w.hasFocus() for w in self._watched))
        elif event.type() == QEvent.Type.EnabledChange:
            repolish(self)
        return super().eventFilter(watched, event)


class SearchBox(QFrame):
    """Header search: icon, line edit and a shortcut hint (Ctrl+K focuses it)."""

    def __init__(self, placeholder: str, shortcut: str = "Ctrl+K", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("searchBox")
        row = QHBoxLayout(self)
        row.setContentsMargins(10, 0, 6, 0)
        row.setSpacing(8)
        glyph = QLabel(self)
        glyph.setPixmap(icons.icon("search", tk.TEXT_3).pixmap(QSize(15, 15)))
        row.addWidget(glyph)
        self.edit = QLineEdit(self)
        self.edit.setObjectName("searchInput")
        self.edit.setPlaceholderText(placeholder)
        row.addWidget(self.edit, 1)
        self.hint = QLabel(shortcut.replace("+", " "), self)
        self.hint.setObjectName("kbd")
        row.addWidget(self.hint)
        if shortcut:
            target = parent.window() if parent is not None else self
            self.shortcut = QShortcut(QKeySequence(shortcut), target)
            self.shortcut.activated.connect(self.focus)

    def focus(self) -> None:
        self.edit.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.edit.selectAll()
