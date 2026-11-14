"""Read-only voltage and current panel for a multichannel load."""

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QHeaderView, QTreeWidget, QTreeWidgetItem

from lab_control.ui.pages.monitoring.power_plot import CHANNEL_COLORS


class PowerReadouts(QTreeWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setRootIsDecorated(False)
        self.setSelectionMode(QTreeWidget.SelectionMode.NoSelection)
        self.setEditTriggers(QTreeWidget.EditTrigger.NoEditTriggers)
        self.setHeaderLabels(["通道", "电压 (V)", "电流 (A)"])
        self.header().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.rows: dict[int, QTreeWidgetItem] = {}
        for column in (1, 2):
            self.headerItem().setTextAlignment(
                column, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            )

    def set_channels(self, channels: tuple[int, ...]) -> None:
        self.clear()
        self.rows.clear()
        for channel in channels:
            item = QTreeWidgetItem(self, [f"CH {channel}", "—", "—"])
            item.setForeground(0, QColor(CHANNEL_COLORS[(channel - 1) % len(CHANNEL_COLORS)]))
            for column in (1, 2):
                item.setTextAlignment(column, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.rows[channel] = item

    def show_measurements(self, frame, channels: tuple[int, ...]) -> None:
        values = {(m.channel, m.quantity): m for m in frame.batch.measurements} if frame else {}
        for channel, item in self.rows.items():
            for column, quantity in ((1, "voltage"), (2, "current")):
                reading = values.get((channel, quantity)) if channel in channels else None
                item.setText(column, format(reading.values[0], ".6g") if reading is not None else "—")
