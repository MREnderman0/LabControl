"""Configure the oscilloscope record length and show its confirmed readback."""

from math import isfinite

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QGridLayout, QLabel, QPushButton, QWidget

from lab_control.domain.instruments import DeviceCommand
from lab_control.domain.oscilloscope import engineering
from lab_control.domain.readback import boolean_value
from lab_control.ui.widgets.workspace_combo import WorkspaceComboBox


class ScopeDepth(QWidget):
    requested = Signal(object)

    def __init__(self):
        super().__init__()
        self.baseline = None
        self.editable = False
        grid = QGridLayout(self)
        grid.setContentsMargins(0, 3, 0, 4)
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(5)
        grid.setColumnStretch(1, 1)
        self.depth_choice = WorkspaceComboBox()
        self.depth_choice.setAccessibleName("采集深度")
        self.depth_choice.setToolTip("示波器每通道的存储长度。软件每通道单帧最多读取 4 M 点。")
        self.apply = QPushButton("应用")
        self.apply.setObjectName("primaryButton")
        self.apply.setProperty("buttonSize", "panel")
        self.actual = QLabel("等待读回")
        self.actual.setObjectName("rangeHint")
        self.actual.setWordWrap(True)
        grid.addWidget(QLabel("采集深度"), 0, 0)
        grid.addWidget(self.depth_choice, 0, 1)
        grid.addWidget(self.apply, 0, 2)
        grid.addWidget(self.actual, 1, 0, 1, 3)
        self.depth_choice.currentIndexChanged.connect(self.validate)
        self.apply.clicked.connect(self.commit)
        self.setEnabled(False)

    def sync(self, context, values, caps, available):
        points = values.get((1, "record_length"))
        auto = boolean_value(values.get((1, "record_length_auto")))
        names = ("record_length", "record_length_auto")
        ready = all(caps.get(n) and caps[n].available and caps[n].writable for n in names)
        ready = (
            ready
            and isinstance(points, (int, float))
            and isfinite(points)
            and points > 0
            and auto is not None
        )
        self.editable = bool(available and ready)
        self.setEnabled(self.editable)
        if not ready:
            self.baseline = None
            self.actual.setText("等待读回")
            self.depth_choice.clear()
            return
        depth_spec = caps["record_length"].parameters["value"]
        state = (context, int(points), auto, depth_spec)
        if state != self.baseline:
            self.baseline = state
            self.depth_choice.blockSignals(True)
            self.depth_choice.clear()
            self.depth_choice.addItem("自动", "auto")
            for choice in depth_spec.choices:
                n = int(choice)
                if (depth_spec.minimum or 0) <= n <= (depth_spec.maximum or n):
                    self.depth_choice.addItem(engineering(n, "点"), n)
            if not auto and self.depth_choice.findData(int(points)) < 0:
                self.depth_choice.addItem(f"{points:,.0f} 点（当前）", int(points))
            self.depth_choice.setCurrentIndex(self.depth_choice.findData("auto" if auto else int(points)))
            self.depth_choice.blockSignals(False)
            self.actual.setText(f"实际深度 {engineering(points, '点')}")
        self.validate()

    def selected_command(self):
        if not self.baseline or not self.editable:
            return None
        _, points, auto, _ = self.baseline
        selected = self.depth_choice.currentData()
        if selected == "auto":
            if not auto:
                return DeviceCommand("record_length_auto", {"value": True})
        elif selected is not None and (auto or selected != points):
            return DeviceCommand("record_length", {"value": selected})
        return None

    def validate(self):
        self.apply.setEnabled(self.selected_command() is not None)

    def commit(self):
        command = self.selected_command()
        if command is not None:
            self.requested.emit(command)
