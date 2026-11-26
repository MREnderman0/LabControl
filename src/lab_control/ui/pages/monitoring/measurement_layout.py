"""Classic measurement chart and parameter groups shared by instrument workspaces."""

from PySide6.QtWidgets import QFrame, QGridLayout, QHBoxLayout, QLabel, QMenu, QToolButton, QVBoxLayout

from .recording_state import RecordingButton
from .timing import MonitoringInterval


class MeasurementChart(QFrame):
    def __init__(self, title, parent=None, *, spectrum=False):
        super().__init__(parent)
        self.setObjectName("spectrumChart")
        self.setMinimumWidth(280)
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(10, 10 if spectrum else 8, 10, 10 if spectrum else 8)
        self.toolbar = QHBoxLayout()
        heading = QLabel(title, self)
        heading.setObjectName("spectrumChartTitle")
        self.toolbar.addWidget(heading)
        self.toolbar.addStretch()
        self.record = RecordingButton(self)
        self.toolbar.addWidget(self.record)
        self.body.addLayout(self.toolbar)

    def add_interval(self, controller) -> MonitoringInterval:
        interval = MonitoringInterval(controller, self)
        interval.setFixedWidth(self.record.width())
        self.toolbar.addWidget(interval)
        return interval

    def scope_tools(self) -> QMenu:
        tools = QToolButton(self)
        tools.setObjectName("curveTools")
        tools.setProperty("buttonSize", "compact")
        tools.setText("曲线工具")
        tools.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(tools)
        tools.setMenu(menu)
        self.toolbar.addWidget(tools)
        return menu

    def add_status(self):
        footer = QHBoxLayout()
        status = QLabel("监测未开始", self)
        status.setObjectName("mutedText")
        status.setWordWrap(True)
        footer.addWidget(status, 1)
        actual = QLabel("实际 — s/帧", self)
        actual.setObjectName("rangeHint")
        footer.addWidget(actual)
        self.body.addLayout(footer)
        return status, actual


def scope_section(stack, title):
    frame = QFrame()
    frame.setObjectName("scopeSection")
    box = QVBoxLayout(frame)
    box.setContentsMargins(10, 5, 10, 5)
    box.setSpacing(2)
    if title:
        label = QLabel(title)
        label.setObjectName("spectrumChartTitle")
        box.addWidget(label)
    stack.addWidget(frame)
    return box


def spectrum_grid(column):
    grid = QGridLayout()
    grid.setContentsMargins(0, 0, 0, 0)
    grid.setHorizontalSpacing(10)
    grid.setVerticalSpacing(12)
    grid.setColumnStretch(0, 1)
    grid.setColumnStretch(1, 1)
    column.addLayout(grid)
    return grid
