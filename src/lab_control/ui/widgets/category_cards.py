"""Overview category cards: counts, a status bar and the first connected devices."""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from lab_control.ui.styles import tokens as tk
from lab_control.ui.widgets import icons
from lab_control.ui.widgets.graphite import Dot, ElideLabel

DEVICE_ROWS = 3
STATUS_DOT = {
    "connected": "ok",
    "busy": "accent",
    "connecting": "warn",
    "recovering": "warn",
    "degraded": "err",
    "error": "err",
}


class IconTile(QWidget):
    def __init__(self, icon_name: str, size: int = 34) -> None:
        super().__init__()
        self.icon_name = icon_name
        self.setFixedSize(size, size)

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(tk.ACCENT_SOFT))
        painter.drawRoundedRect(QRectF(self.rect()), 9, 9)
        inner = round(self.width() * 0.52)
        offset = (self.width() - inner) / 2
        icons.paint(painter, self.icon_name, tk.ACCENT, QRectF(offset, offset, inner, inner))


class StatusBar(QWidget):
    """Proportion of abnormal / online / offline devices."""

    def __init__(self) -> None:
        super().__init__()
        self.setFixedHeight(5)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.segments: tuple[tuple[str, int], ...] = ()

    def set_counts(self, errors: int, online: int, offline: int) -> None:
        segments = tuple(
            (c, n) for c, n in ((tk.ERR, errors), (tk.OK, online), (tk.BORDER_STRONG, offline)) if n > 0
        )
        if segments != self.segments:
            self.segments = segments
            self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        segments = self.segments or ((tk.BORDER_STRONG, 1),)
        total = sum(n for _, n in segments)
        gap = 3
        width = self.width() - gap * (len(segments) - 1)
        x = 0.0
        for color, count in segments:
            w = width * count / total
            painter.setBrush(QColor(color))
            painter.drawRoundedRect(QRectF(x, 0, w, self.height()), 2.5, 2.5)
            x += w + gap


class CategoryCard(QFrame):
    clicked = Signal(str)

    def __init__(self, category: str, title: str, description: str, parent=None) -> None:
        super().__init__(parent)
        self.category = category
        self.setObjectName("categoryCard")
        self.setFixedHeight(204)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName(title)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 12)
        layout.setSpacing(10)

        header = QHBoxLayout()
        header.setSpacing(12)
        header.addWidget(IconTile(icons.CATEGORY_ICONS.get(category, "box")))
        names = QVBoxLayout()
        names.setSpacing(1)
        heading = QLabel(title)
        heading.setObjectName("cardTitle")
        names.addWidget(heading)
        description_label = ElideLabel(description)
        description_label.setObjectName("cardDescription")
        names.addWidget(description_label)
        header.addLayout(names, 1)
        count = QVBoxLayout()
        count.setSpacing(0)
        self.online_count = QLabel()
        self.online_count.setObjectName("cardValue")
        self.online_count.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.online_count.setToolTip("在线设备")
        self.visible_count = QLabel()
        self.visible_count.setObjectName("cardVisibility")
        self.visible_count.setAlignment(Qt.AlignmentFlag.AlignRight)
        count.addWidget(self.online_count)
        count.addWidget(self.visible_count)
        header.addLayout(count)
        layout.addLayout(header)

        self.status_bar = StatusBar()
        layout.addWidget(self.status_bar)

        self.device_grid = QGridLayout()
        self.device_grid.setHorizontalSpacing(8)
        self.device_grid.setVerticalSpacing(0)
        self.device_grid.setColumnStretch(1, 1)
        self.device_rows: list[tuple[Dot, ElideLabel, ElideLabel, QLabel]] = []
        for row in range(DEVICE_ROWS):
            dot = Dot("idle", 7, box=12)
            name = ElideLabel()
            name.setObjectName("cardDeviceName")
            detail = ElideLabel()
            detail.setObjectName("cardDeviceDetail")
            state = QLabel()
            state.setObjectName("cardDeviceState")
            state.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            for column, widget in enumerate((dot, name, detail, state)):
                self.device_grid.addWidget(widget, row, column)
            self.device_grid.setRowMinimumHeight(row, 24)
            self.device_grid.setColumnStretch(2, 1)
            self.device_rows.append((dot, name, detail, state))
        layout.addLayout(self.device_grid)
        layout.addStretch(1)

        metrics = QHBoxLayout()
        metrics.setSpacing(14)
        self.discovered_count = QLabel()
        self.discovered_count.setObjectName("cardMeta")
        metrics.addWidget(self.discovered_count)
        self.metrics: dict[str, QLabel] = {}
        for key, caption_text in (("disconnected", "未连接"), ("errors", "异常"), ("bound", "已绑定")):
            caption = QLabel(caption_text)
            caption.setObjectName("metricCaption")
            number = QLabel()
            number.setObjectName("metricValue")
            self.metrics[key] = number
            cell = QHBoxLayout()
            cell.setSpacing(4)
            cell.addWidget(caption)
            cell.addWidget(number)
            metrics.addLayout(cell)
        metrics.addStretch()
        link = QLabel("打开设备池 →")
        link.setObjectName("cardLinkText")
        metrics.addWidget(link)
        layout.addLayout(metrics)
        for label in self.findChildren(QWidget):
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.update_counts(0, 0, 0, 0)
        self.set_devices(())

    def update_counts(self, discovered: int, online: int, errors: int, bound: int) -> None:
        self.visible_count.setText(f"可见 {discovered} 台")
        self.discovered_count.setText(f"发现 {discovered} 台")
        self.online_count.setText(f"{online:02d}")
        for key, value in {"disconnected": discovered - online, "errors": errors, "bound": bound}.items():
            label = self.metrics[key]
            label.setText(str(value))
            alert = key == "errors" and value > 0
            if label.property("alert") != alert:
                label.setProperty("alert", alert)
                label.style().unpolish(label)
                label.style().polish(label)
        self.status_bar.set_counts(errors, max(online - errors, 0), max(discovered - online, 0))

    def set_devices(self, devices) -> None:
        """devices: (connection, name, detail, state text) for at most three devices."""
        devices = tuple(devices)[:DEVICE_ROWS]
        for index, (dot, name, detail, state) in enumerate(self.device_rows):
            visible = index < len(devices)
            for widget in (dot, name, detail, state):
                widget.setVisible(visible)
            if not visible:
                continue
            connection, title, info, state_text = devices[index]
            dot.set_status(STATUS_DOT.get(connection, "idle"), live=connection == "busy")
            if name.text() != title:
                name.setText(title)
            if detail.text() != info:
                detail.setText(info)
            if state.text() != state_text:
                state.setText(state_text)
                state.setProperty("alert", connection in {"error", "degraded"})
                state.style().unpolish(state)
                state.style().polish(state)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.category)
            event.accept()
            return
        super().mousePressEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() in {Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space}:
            self.clicked.emit(self.category)
            event.accept()
        else:
            super().keyPressEvent(event)


class CategoryCardGrid(QScrollArea):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("categoryGrid")
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        body.setObjectName("categoryGridBody")
        self.grid = QGridLayout(body)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(14)
        self.grid.setVerticalSpacing(14)
        self.grid.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.setWidget(body)
        self.cards: tuple[CategoryCard, ...] = ()
        self.columns = 0

    def set_cards(self, cards: tuple[CategoryCard, ...]) -> None:
        if self.cards != cards:
            for card in self.cards:
                self.grid.removeWidget(card)
                card.hide()
            self.cards = cards
            self.columns = 0
        self.reflow()

    def reflow(self) -> None:
        columns = max(1, min(3, (self.viewport().width() + 14) // 330))
        if columns == self.columns:
            return
        for card in self.cards:
            self.grid.removeWidget(card)
        for index, card in enumerate(self.cards):
            self.grid.addWidget(card, index // columns, index % columns)
            card.show()
        for column in range(3):
            self.grid.setColumnStretch(column, int(column < columns))
        self.columns = columns

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self.reflow()
