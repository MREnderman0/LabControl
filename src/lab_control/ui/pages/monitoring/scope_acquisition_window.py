"""Edit the oscilloscope's full horizontal span and hardware record length together."""

from math import isclose, isfinite

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QGridLayout, QLabel, QLineEdit, QPushButton, QWidget

from lab_control.domain.instruments import DeviceCommand, ValidationError
from lab_control.domain.oscilloscope import engineering
from lab_control.domain.readback import boolean_value
from lab_control.ui.widgets.workspace_combo import WorkspaceComboBox


class AcquisitionWindow(QWidget):
    requested = Signal(object)

    def __init__(self):
        super().__init__()
        self.baseline = None
        self.editable = False
        self.factor = 1.0
        grid = QGridLayout(self)
        grid.setContentsMargins(0, 3, 0, 4)
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(5)
        grid.setColumnStretch(1, 1)
        self.duration = QLineEdit()
        self.duration.setAccessibleName("采集时间窗口")
        self.duration.setPlaceholderText("时间跨度")
        self.unit = WorkspaceComboBox()
        self.unit.setAccessibleName("时间窗口单位")
        for unit, factor in (("ns", 1e-9), ("µs", 1e-6), ("ms", 1e-3), ("s", 1.0)):
            self.unit.addItem(unit, factor)
        self.depth_choice = WorkspaceComboBox()
        self.depth_choice.setAccessibleName("采集深度")
        self.depth_choice.setToolTip("示波器每通道的存储长度。软件每通道单帧最多读取 4 M 点。")
        self.apply = QPushButton("应用")
        self.apply.setObjectName("primaryButton")
        self.apply.setProperty("buttonSize", "panel")
        self.actual = QLabel("等待读回")
        self.actual.setObjectName("rangeHint")
        self.actual.setWordWrap(True)
        grid.addWidget(QLabel("时间窗口"), 0, 0)
        grid.addWidget(self.duration, 0, 1)
        grid.addWidget(self.unit, 0, 2)
        grid.addWidget(QLabel("采集深度"), 1, 0)
        grid.addWidget(self.depth_choice, 1, 1, 1, 2)
        grid.addWidget(self.actual, 2, 0, 1, 2)
        grid.addWidget(self.apply, 2, 2)
        self.duration.textChanged.connect(self.validate)
        self.unit.currentIndexChanged.connect(self.change_unit)
        self.depth_choice.currentIndexChanged.connect(self.validate)
        self.apply.clicked.connect(self.commit)
        self.duration.returnPressed.connect(self.commit)
        self.setEnabled(False)

    def change_unit(self):
        factor = self.unit.currentData()
        try:
            value = float(self.duration.text()) * self.factor
        except ValueError:
            value = None
        self.factor = factor
        if value is not None and isfinite(value):
            self.duration.setText(f"{value / factor:.12g}")
        self.validate()

    def sync(self, context, values, caps, available):
        scale = values.get((1, "timebase"))
        divisions = values.get((1, "horizontal_divisions"))
        points = values.get((1, "record_length"))
        auto = boolean_value(values.get((1, "record_length_auto")))
        names = ("timebase", "record_length", "record_length_auto")
        ready = all(caps.get(n) and caps[n].available and caps[n].writable for n in names)
        ready = (
            ready
            and all(isinstance(v, (int, float)) and isfinite(v) and v > 0 for v in (scale, divisions, points))
            and auto is not None
        )
        self.editable = bool(available and ready)
        self.setEnabled(self.editable)
        if not ready:
            self.baseline = None
            self.actual.setText("等待读回")
            self.duration.clear()
            self.depth_choice.clear()
            return
        time_spec = caps["timebase"].parameters["value"]
        depth_spec = caps["record_length"].parameters["value"]
        state = (context, scale, divisions, int(points), auto, time_spec, depth_spec)
        if state != self.baseline:
            self.baseline = state
            span = scale * divisions
            factor = next((f for f in (1.0, 1e-3, 1e-6) if span >= f), 1e-9)
            self.unit.blockSignals(True)
            self.unit.setCurrentIndex(self.unit.findData(factor))
            self.unit.blockSignals(False)
            self.factor = factor
            self.duration.setText(f"{span / factor:.12g}")
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
            self.actual.setText(f"实测 {engineering(span, 's')} · {engineering(points, '点')}")
            self.duration.setToolTip(f"整屏 {divisions:g} 格 · 当前时基 {engineering(scale, 's')}/格")
        self.validate()

    def commands(self):
        if not self.baseline or not self.editable:
            return []
        _, previous, divisions, points, auto, spec, _ = self.baseline
        try:
            scale = float(self.duration.text()) * self.factor / divisions
            spec.validate("时间窗口", scale)
        except (ValueError, ValidationError):
            return []
        selected = self.depth_choice.currentData()
        if selected is None:
            return []
        result = []
        if not isclose(scale, previous, rel_tol=1e-10):
            result.append(DeviceCommand("timebase", {"value": scale}))
        if selected == "auto" and not auto:
            result.append(DeviceCommand("record_length_auto", {"value": True}))
        elif selected != "auto" and (auto or selected != points):
            result.append(DeviceCommand("record_length", {"value": selected}))
        return result

    def validate(self):
        self.apply.setEnabled(bool(self.commands()))

    def commit(self):
        commands = self.commands()
        if commands:
            self.requested.emit(commands)
