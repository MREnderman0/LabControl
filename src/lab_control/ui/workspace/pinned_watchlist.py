"""Read-only presentation of the workspace's pinned watch preset on Home."""

from PySide6.QtCore import QRect, QSize, Qt
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import QAbstractItemView, QLabel, QListView, QStyledItemDelegate

from lab_control.ui.kit import Card, ElideLabel, button
from lab_control.ui.kit.base import plain_tooltip
from lab_control.ui.pages.watch_table import WatchTableModel
from lab_control.ui.styles import tokens as tk


def reading_lines(index):
    device, channel, metric, value, unit = (index.siblingAtColumn(i).data() for i in range(5))
    return f"{channel} · {metric}", device, f"{value} {unit}".strip(), index.siblingAtColumn(8).data()


class HomeReadingsModel(WatchTableModel):
    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if index.isValid() and role in (Qt.ItemDataRole.AccessibleTextRole, Qt.ItemDataRole.ToolTipRole):
            label, device, value, status = reading_lines(index)
            text = f"{device}\n{label}\n{value} · {status}"
            return plain_tooltip(text) if role == Qt.ItemDataRole.ToolTipRole else text
        return super().data(index, role)


class ReadingDelegate(QStyledItemDelegate):
    """Keep values aligned on the right and fit device names independently of the readings."""

    def sizeHint(self, option, index):  # noqa: N802
        return QSize(100, 2 * option.fontMetrics.height() + 18)

    def paint(self, painter, option, index):
        label, device, value, status = reading_lines(index)
        rect = option.rect.adjusted(10, 5, -10, -5)
        right_width = min(rect.width() // 2, max(110, option.fontMetrics.horizontalAdvance(value) + 10))
        left_width = rect.width() - right_width - 8
        height = rect.height() // 2
        reading_color = index.siblingAtColumn(3).data(Qt.ItemDataRole.ForegroundRole)
        status_color = reading_color or QColor(tk.OK)
        painter.save()
        for text, right, second, color in (
            (label, False, False, QColor(tk.TEXT)),
            (device, False, True, QColor(tk.TEXT_2)),
            (value, True, False, reading_color or QColor(tk.TEXT)),
            (status, True, True, status_color),
        ):
            font = QFont(option.font)
            if not second:
                font.setWeight(QFont.Weight.DemiBold)
            painter.setFont(font)
            painter.setPen(color)
            width = right_width if right else left_width
            x = rect.right() - width + 1 if right else rect.left()
            box = QRect(x, rect.top() + (height if second else 0), width, height)
            align = Qt.AlignmentFlag.AlignRight if right else Qt.AlignmentFlag.AlignLeft
            shown = painter.fontMetrics().elidedText(text, Qt.TextElideMode.ElideRight, width)
            painter.drawText(box, align | Qt.AlignmentFlag.AlignVCenter, shown)
        painter.setPen(QColor(tk.BORDER))
        painter.drawLine(rect.left(), option.rect.bottom(), rect.right(), option.rect.bottom())
        painter.restore()


class PinnedWatchlistCard(Card):
    def __init__(self, window):
        super().__init__("状态监视", padding=(6, 2, 6, 6), spacing=0, object_name="overviewSide")
        self.hub = window.watch_hub
        self.header().addStretch()
        self.open_button = button("打开监视列表", "link", size="sm")
        self.header().addWidget(self.open_button)
        self.model = HomeReadingsModel(self.hub, self)
        self.open_button.clicked.connect(lambda: window.open_watchlist(self.model.preset))
        self.preset_name = ElideLabel()
        self.preset_name.setObjectName("taskTitle")
        self.preset_name.setContentsMargins(10, 6, 10, 4)
        self.body.addWidget(self.preset_name)
        self.empty = QLabel()
        self.empty.setObjectName("taskMeta")
        self.empty.setContentsMargins(10, 10, 10, 10)
        self.empty.setWordWrap(True)
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.body.addWidget(self.empty, 1)
        self.list = QListView()
        self.list.setObjectName("homeWatchReadings")
        self.list.setAccessibleName("置顶预设读数")
        self.list.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.list.setUniformItemSizes(True)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.list.setModel(self.model)
        self.list.setItemDelegate(ReadingDelegate(self.list))
        self.body.addWidget(self.list, 1)
        self.hub.changed.connect(self.reload)
        self.reload()

    def reload(self):
        preset = self.hub.presets.pinned
        self.preset_name.setText(preset.name if preset else "")
        self.preset_name.setVisible(preset is not None)
        self.empty.setText(
            "此预设还没有监视项，请打开监视列表添加。" if preset else "请在监视列表中选择一个预设置顶。"
        )
        rows = len(preset.rows) if preset else 0
        self.list.setVisible(bool(rows))
        self.empty.setVisible(not rows)
        if self.isVisible():
            key = preset.id if preset else ""
            self.model.bind(key)
            self.hub.subscribe(self, key)

    def showEvent(self, event):
        super().showEvent(event)
        self.reload()

    def hideEvent(self, event):
        self.hub.subscribe(self, "")
        self.model.bind("")
        super().hideEvent(event)
