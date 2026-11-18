"""Containers: card with header/body/footer, group section, empty state and hint."""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from lab_control.ui.kit import icons
from lab_control.ui.kit.base import Pill, set_property
from lab_control.ui.styles import tokens as tk


class Card(QFrame):
    """Surface card. ``head``/``foot`` rows are created on demand and separated by 1 px rules.

    ``body`` is the content layout; ``padding`` applies to it only, so readout strips and plots
    can sit edge to edge (``padding=0``).
    """

    clicked = Signal()

    def __init__(
        self,
        title: str = "",
        parent: QWidget | None = None,
        *,
        padding: int | tuple[int, int, int, int] = 16,
        spacing: int = 10,
        hoverable: bool = False,
        object_name: str = "kitCard",
    ) -> None:
        super().__init__(parent)
        self.setObjectName(object_name)
        self.setProperty("kitCard", True)
        self.setProperty("hoverable", hoverable)
        self._hoverable = hoverable
        if hoverable:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.outer = QVBoxLayout(self)
        self.outer.setContentsMargins(0, 0, 0, 0)
        self.outer.setSpacing(0)
        self.head: QFrame | None = None
        self.head_row: QHBoxLayout | None = None
        self.title: QLabel | None = None
        self.foot: QFrame | None = None
        self.foot_row: QHBoxLayout | None = None
        self.content = QWidget(self)
        self.content.setObjectName("cardBody")
        self.body = QVBoxLayout(self.content)
        margins = (padding,) * 4 if isinstance(padding, int) else padding
        self.body.setContentsMargins(*margins)
        self.body.setSpacing(spacing)
        self.outer.addWidget(self.content, 1)
        if title:
            self.header(title)

    def header(self, title: str = "", *, margins=(16, 0, 10, 0), height: int = 48) -> QHBoxLayout:
        if self.head_row is None:
            self.head = QFrame(self)
            self.head.setObjectName("cardHead")
            self.head.setFixedHeight(height)
            self.head_row = QHBoxLayout(self.head)
            self.head_row.setContentsMargins(*margins)
            self.head_row.setSpacing(8)
            self.outer.insertWidget(0, self.head)
            if title:
                self.title = QLabel(title, self.head)
                self.title.setObjectName("cardTitle")
                self.head_row.addWidget(self.title)
        return self.head_row

    def footer(self, *, margins=(16, 10, 16, 10)) -> QHBoxLayout:
        if self.foot_row is None:
            self.foot = QFrame(self)
            self.foot.setObjectName("cardFoot")
            self.foot_row = QHBoxLayout(self.foot)
            self.foot_row.setContentsMargins(*margins)
            self.foot_row.setSpacing(8)
            self.outer.addWidget(self.foot)
        return self.foot_row

    def add_status(self, text: str, kind: str = "neutral", *, dot: bool = True) -> Pill:
        pill = Pill(text, kind, dot=dot)
        self.header().addWidget(pill)
        return pill

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if (
            self._hoverable
            and event.button() == Qt.MouseButton.LeftButton
            and self.rect().contains(event.position().toPoint())
        ):
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class Section(QFrame):
    """Titled group inside a panel (频率 / 幅度 / 带宽), separated by a bottom rule."""

    def __init__(self, title: str = "", parent: QWidget | None = None, *, last: bool = False) -> None:
        super().__init__(parent)
        self.setObjectName("panelSection")
        self.setProperty("last", last)
        self.layout_ = QVBoxLayout(self)
        self.layout_.setContentsMargins(16, 12, 16, 14)
        self.layout_.setSpacing(10)
        self.title = QLabel(title, self)
        self.title.setObjectName("sectionTitle")
        self.title.setVisible(bool(title))
        self.layout_.addWidget(self.title)

    def set_last(self, last: bool) -> None:
        set_property(self, "last", last)


class EmptyState(QFrame):
    """Dashed placeholder with icon, title, text and an optional action row."""

    def __init__(
        self, title: str, text: str = "", icon: str = "inbox", parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setObjectName("emptyState")
        column = QVBoxLayout(self)
        column.setContentsMargins(24, 28, 24, 28)
        column.setSpacing(8)
        column.setAlignment(Qt.AlignmentFlag.AlignCenter)
        glyph = QLabel(self)
        glyph.setPixmap(icons.icon(icon, tk.TEXT_3).pixmap(QSize(28, 28)))
        glyph.setAlignment(Qt.AlignmentFlag.AlignCenter)
        column.addWidget(glyph)
        self.title = QLabel(title, self)
        self.title.setObjectName("emptyTitle")
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        column.addWidget(self.title)
        self.text = QLabel(text, self)
        self.text.setObjectName("emptyText")
        self.text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.text.setWordWrap(True)
        self.text.setVisible(bool(text))
        column.addWidget(self.text)
        self.action_row = QHBoxLayout()
        self.action_row.setAlignment(Qt.AlignmentFlag.AlignCenter)
        column.addLayout(self.action_row)

    def set_text(self, title: str, text: str = "") -> None:
        self.title.setText(title)
        self.text.setText(text)
        self.text.setVisible(bool(text))


class Hint(QFrame):
    """Soft accent note with an icon (operating hints, not errors)."""

    def __init__(
        self, text: str, icon: str = "info", parent: QWidget | None = None, kind: str = "accent"
    ) -> None:
        super().__init__(parent)
        self.setObjectName("kitHint")
        self.setProperty("kind", kind)
        row = QHBoxLayout(self)
        row.setContentsMargins(10, 7, 10, 7)
        row.setSpacing(8)
        glyph = QLabel(self)
        color = tk.ACCENT if kind == "accent" else tk.STATUS.get(kind, tk.TEXT_2)
        glyph.setPixmap(icons.icon(icon, color).pixmap(QSize(14, 14)))
        glyph.setAlignment(Qt.AlignmentFlag.AlignTop)
        row.addWidget(glyph)
        self.label = QLabel(text, self)
        self.label.setObjectName("hintText")
        self.label.setWordWrap(True)
        row.addWidget(self.label, 1)
