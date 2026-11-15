"""PicoScope input drafts and explicit capture preparation in the oscilloscope workspace."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable

from PySide6.QtCore import QSignalBlocker, QSize, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSlider,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from lab_control.domain.instruments import DeviceCommand, DeviceError
from lab_control.domain.oscilloscope import COLORS, engineering
from lab_control.ui.pages.monitoring.oscilloscope_controls import gear_icon
from lab_control.ui.widgets.workspace_combo import WorkspaceComboBox


def choice(items):
    box = WorkspaceComboBox()
    for label, value in items:
        box.addItem(label, value)
    return box


def number(spec, value, suffix="", *, integer=False):
    field = QSpinBox() if integer else QDoubleSpinBox()
    if not integer:
        field.setDecimals(6)
    field.setRange(spec.minimum, spec.maximum)
    field.setValue(value)
    field.setSuffix(suffix)
    field.setKeyboardTracking(False)
    return field


class InputRange(QSlider):
    """The SDK exposes discrete full-scale input ranges, in volts."""

    def __init__(self, choices):
        super().__init__(Qt.Orientation.Horizontal)
        self.choices = tuple(choices)
        self.setRange(0, len(self.choices) - 1)
        self.setAccessibleName("输入量程")

    def currentData(self):
        return self.choices[self.value()]

    def findData(self, value):
        return self.choices.index(value) if value in self.choices else -1

    def setCurrentIndex(self, index):
        if index >= 0:
            self.setValue(index)


class PicoScopeControls(QFrame):
    """Editing changes only the draft. Start and Once prepare the selected inputs."""

    channels_changed = Signal()
    source_changed = Signal()
    busy_changed = Signal(bool)

    def __init__(self, controller, device_id, draft, parent=None):
        super().__init__(parent)
        self.setObjectName("scopeControls")
        self.controller, self.device_id = controller, device_id
        caps = controller.declared_capabilities(device_id)
        self.instrument_channels = caps.channels
        self.running = False
        self.restart = False
        self.error = ""
        self.checks = {}
        self.ranges, self.couplings, self.offsets = {}, {}, {}
        self.session_id = ""
        self._on_ready: Callable[[], None] | None = None
        self._draft_commands = []
        self._current = {}
        self._reads, self._commands = deque(), deque()
        self._stop_revision = 0
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.health = QLabel()
        self.health.setObjectName("spectrumHealth")
        self.health.setWordWrap(True)
        layout.addWidget(self.health)
        self.scroller = QScrollArea()
        self.scroller.setWidgetResizable(True)
        self.scroller.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        stack = QVBoxLayout(body)
        stack.setContentsMargins(0, 0, 6, 0)
        stack.setSpacing(7)

        def section(title):
            panel = QFrame()
            panel.setObjectName("scopeSection")
            form = QFormLayout(panel)
            form.setContentsMargins(10, 6, 10, 6)
            form.setSpacing(4)
            form.addRow(title)
            stack.addWidget(panel)
            return form

        title = QLabel("水平")
        title.setObjectName("spectrumChartTitle")
        form = section(title)
        self.capture_mode = choice((("块采样", "waveform"), ("流采样", "streaming")))
        form.addRow("采集方式", self.capture_mode)
        self.points = QSpinBox()
        self.points.setRange(1, 4_000_000)
        self.points.setValue(2000)
        form.addRow("采集点数", self.points)
        self.timebase = number(caps.get("timebase").parameters["value"], 10, integer=True)
        form.addRow("时基索引", self.timebase)
        self.sample_interval = QDoubleSpinBox()
        self.sample_interval.setDecimals(3)
        interval_spec = caps.get("sample_interval").parameters["value"]
        self.sample_interval.setRange(interval_spec.minimum * 1e6, interval_spec.maximum * 1e6)
        self.sample_interval.setValue(1)
        self.sample_interval.setSuffix(" µs")
        form.addRow("采样间隔", self.sample_interval)
        self.sample_rate = QLabel("实际采样率　—")
        self.sample_rate.setObjectName("scopeSampleRate")
        form.addRow(self.sample_rate)
        self.horizontal = form
        self.range_labels = {}
        self.offset_sliders = {}
        for ch in caps.channels:
            channel = controller.declared_capabilities(device_id, ch).get("channel").parameters
            check = QCheckBox(f"CH {ch}")
            color = COLORS[(ch - 1) % len(COLORS)]
            check.setStyleSheet(
                f"QCheckBox {{ color: {color}; font-weight: 600; }}"
                f"QCheckBox::indicator:checked {{ background: {color}; border-color: {color}; }}"
            )
            check.setChecked(ch == 1)
            self.checks[ch] = check
            box = section(check)
            self.ranges[ch] = InputRange(channel["range"].choices)
            self.ranges[ch].setCurrentIndex(self.ranges[ch].findData("10"))
            self.couplings[ch] = choice(
                ({"AC": "交流", "DC": "直流（高阻）", "DC50": "直流（50 Ω）"}[v], v)
                for v in channel["coupling"].choices
            )
            self.couplings[ch].setCurrentIndex(self.couplings[ch].findData("DC"))
            self.offsets[ch] = number(channel["offset"], 0, " V")
            row = QHBoxLayout()
            row.addWidget(self.ranges[ch], 1)
            self.range_labels[ch] = QLabel()
            self.range_labels[ch].setObjectName("scopeControlValue")
            row.addWidget(self.range_labels[ch])
            box.addRow("量程", row)
            offset = self.offsets[ch]
            offset.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
            offset.setFixedWidth(max(118, offset.fontMetrics().horizontalAdvance("−20.000000 V") + 28))
            slider = self.offset_sliders[ch] = QSlider(Qt.Orientation.Horizontal)
            slider.setRange(-1000, 1000)
            slider.setAccessibleName(f"CH{ch} 模拟偏移")
            slider.valueChanged.connect(
                lambda value, field=offset: field.setValue(value * field.maximum() / 1000)
            )
            offset.valueChanged.connect(lambda _value, ch=ch: self.sync_offset_slider(ch))
            offset_row = QHBoxLayout()
            offset_row.addWidget(slider, 1)
            offset_row.addWidget(offset)
            box.addRow("偏移", offset_row)
            box.addRow("耦合", self.couplings[ch])
            for signal in (
                self.ranges[ch].valueChanged,
                self.couplings[ch].currentIndexChanged,
                check.toggled,
            ):
                signal.connect(lambda *_args, ch=ch: self.update_channel(ch))
            self.update_channel(ch)
            check.toggled.connect(self.channels_changed)

        title = QLabel("触发")
        title.setObjectName("spectrumChartTitle")
        self.gear = QToolButton()
        self.gear.setIcon(gear_icon())
        self.gear.setIconSize(QSize(20, 20))
        self.gear.setAccessibleName("触发设置")
        self.gear.setToolTip("触发通道、边沿与等待时间")
        self.trigger_summary = title
        title_row = QHBoxLayout()
        title_row.addWidget(title, 1)
        title_row.addWidget(self.gear)
        trig = section(title_row)
        self.trigger_dialog = QDialog(self)
        self.trigger_dialog.setWindowTitle("Pico 触发设置")
        settings = QFormLayout(self.trigger_dialog)
        trig_spec = caps.get("trigger").parameters
        self.trigger_mode = choice((("保持当前", None), ("启用边沿触发", True), ("关闭触发", False)))
        self.trigger_channel = choice((f"CH {ch}", ch) for ch in caps.channels)
        self.threshold = number(trig_spec["threshold"], 0, " V")
        self.direction = choice((("上升沿", "RISING"), ("下降沿", "FALLING")))
        self.timeout_us = number(trig_spec["timeout_us"], 100000, " µs", integer=True)
        settings.addRow("模式", self.trigger_mode)
        settings.addRow("触发通道", self.trigger_channel)
        settings.addRow("触发沿", self.direction)
        settings.addRow("自动触发等待", self.timeout_us)
        trig.addRow("电平", self.threshold)
        self.trigger_panel = trig
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.trigger_dialog.accept)
        buttons.rejected.connect(self.trigger_dialog.reject)
        settings.addRow(buttons)
        self.gear.clicked.connect(self.edit_trigger)
        self.trigger_form = settings
        stack.addStretch()
        self.scroller.setWidget(body)
        layout.addWidget(self.scroller)
        if not draft:
            self.restore_session_inputs()
        if draft:
            self.capture_mode.setCurrentIndex(self.capture_mode.findData(draft["kind"]))
            self.points.setValue(draft["points"])
            self.timebase.setValue(draft["timebase"])
            self.sample_interval.setValue(draft["interval_us"])
            for ch, values in draft["channels"].items():
                self.checks[ch].setChecked(values["enabled"])
                for widget, name in ((self.couplings[ch], "coupling"), (self.ranges[ch], "range")):
                    widget.setCurrentIndex(widget.findData(values[name]))
                self.offsets[ch].setValue(values["offset"])
            self.trigger_mode.setCurrentIndex(draft["trigger_mode"])
            self.trigger_channel.setCurrentIndex(self.trigger_channel.findData(draft["trigger_channel"]))
            self.direction.setCurrentIndex(self.direction.findData(draft["direction"]))
            self.threshold.setValue(draft["threshold"])
            self.timeout_us.setValue(draft["timeout_us"])
        self.capture_mode.currentIndexChanged.connect(self.update_mode)
        self.trigger_mode.currentIndexChanged.connect(self.update_trigger_mode)
        self.trigger_channel.currentIndexChanged.connect(self.update_trigger_mode)
        self.direction.currentIndexChanged.connect(self.update_trigger_mode)
        self.update_mode()
        self.applied_draft = None

    def update_channel(self, ch):
        value = self.ranges[ch]
        maximum = (
            value.findData("5") if self.couplings[ch].currentData() == "DC50" else len(value.choices) - 1
        )
        value.setMaximum(maximum)
        input_range = float(value.currentData())
        self.range_labels[ch].setText("±" + engineering(input_range, "V"))
        limit = 1.25 if input_range <= 1 else 20
        self.offsets[ch].setRange(-limit, limit)
        self.sync_offset_slider(ch)
        for widget in (
            value,
            self.range_labels[ch],
            self.couplings[ch],
            self.offsets[ch],
            self.offset_sliders[ch],
        ):
            widget.setEnabled(self.checks[ch].isChecked())

    def sync_offset_slider(self, ch):
        slider, field = self.offset_sliders[ch], self.offsets[ch]
        with QSignalBlocker(slider):
            slider.setValue(round(1000 * field.value() / field.maximum()))

    def edit_trigger(self):
        combos = (self.trigger_mode, self.trigger_channel, self.direction)
        indices = [box.currentIndex() for box in combos]
        timeout = self.timeout_us.value()
        if not self.trigger_dialog.exec():
            for box, index in zip(combos, indices, strict=True):
                box.setCurrentIndex(index)
            self.timeout_us.setValue(timeout)

    def show_sample_rate(self, frame):
        interval = frame.batch.configuration.get("sample_interval_s")
        if interval and interval > 0:
            self.sample_rate.setText(
                f"实际采样率　{engineering(1 / interval, 'Sa/s')}　·　{engineering(interval, 's')}/点"
            )

    def restore_session_inputs(self):
        # Only consume snapshots already published by the session. Changing pages
        # must never initiate acquisition, enable inputs, or write settings.
        session = self.controller.states.get(self.device_id, {}).get("sessionId")
        for (channel, name), frame in self.controller.runtime.sessions.readback_frames(
            self.device_id
        ).items():
            if frame.session_id != session:
                continue
            value = frame.reading.value
            if name == "channel" and channel in self.checks and isinstance(value, dict):
                if set(value) >= {"enabled", "range", "coupling", "offset"}:
                    self.checks[channel].setChecked(bool(value["enabled"]))
                    self.ranges[channel].setCurrentIndex(self.ranges[channel].findData(str(value["range"])))
                    self.couplings[channel].setCurrentIndex(
                        self.couplings[channel].findData(value["coupling"])
                    )
                    self.offsets[channel].setValue(value["offset"])
            if name in {"timebase", "sample_interval"}:
                raw = value.get("value") if isinstance(value, dict) else value
                if isinstance(raw, (int, float)):
                    if name == "timebase":
                        self.timebase.setValue(int(raw))
                    else:
                        self.sample_interval.setValue(raw * 1e6)

    def update_mode(self):
        self.horizontal.setRowVisible(self.timebase, self.capture_mode.currentData() == "waveform")
        self.horizontal.setRowVisible(self.sample_interval, self.capture_mode.currentData() == "streaming")
        self.update_trigger_mode()
        self.source_changed.emit()

    def update_trigger_mode(self):
        enabled = self.trigger_mode.currentData() is True
        for field in (self.trigger_channel, self.direction, self.timeout_us):
            self.trigger_form.setRowVisible(field, enabled)
        self.trigger_panel.setRowVisible(self.threshold, enabled)
        summary = (
            f"CH{self.trigger_channel.currentData()} · {self.direction.currentText()}"
            if enabled
            else self.trigger_mode.currentText()
        )
        self.trigger_summary.setText(f"触发 · {summary}")

    def channels(self):
        return tuple(ch for ch, box in self.checks.items() if box.isChecked())

    def keys(self):
        return ()

    def draft(self):
        return dict(
            kind=self.capture_mode.currentData(),
            points=self.points.value(),
            timebase=self.timebase.value(),
            interval_us=self.sample_interval.value(),
            trigger_mode=self.trigger_mode.currentIndex(),
            trigger_channel=self.trigger_channel.currentData(),
            direction=self.direction.currentData(),
            threshold=self.threshold.value(),
            timeout_us=self.timeout_us.value(),
            channels={
                ch: dict(
                    enabled=box.isChecked(),
                    range=self.ranges[ch].currentData(),
                    coupling=self.couplings[ch].currentData(),
                    offset=self.offsets[ch].value(),
                )
                for ch, box in self.checks.items()
            },
        )

    def desired_commands(self):
        values = self.draft()
        active = self.channels()
        if not active:
            raise DeviceError("请至少选择一个采集通道")
        commands = []
        for ch, params in values["channels"].items():
            commands.append(DeviceCommand("channel", params, ch))
        horizontal = (
            DeviceCommand("timebase", {"value": values["timebase"]})
            if values["kind"] == "waveform"
            else DeviceCommand("sample_interval", {"value": values["interval_us"] * 1e-6})
        )
        commands.append(horizontal)
        trigger = self.trigger_mode.currentData()
        if trigger is not None:
            ch = values["trigger_channel"]
            if ch not in active:
                raise DeviceError("请选择已勾选的通道作为触发通道")
            commands.append(
                DeviceCommand(
                    "trigger",
                    dict(
                        enabled=trigger,
                        threshold=values["threshold"],
                        direction=values["direction"],
                        timeout_us=values["timeout_us"],
                    ),
                    ch,
                )
            )
        for cmd in commands:
            self.controller.capabilities(self.device_id, cmd.channel).get(cmd.operation).validate(
                cmd.parameters
            )
        return commands

    def prepare(self, ready):
        if self.running:
            return
        try:
            self.error = ""
            self._draft_commands = self.desired_commands()
            self.controller.ensure_applicable(self.device_id)
            self.controller.begin_group(self.device_id)
            self.running = True
            self.session_id = self.controller.states[self.device_id]["sessionId"]
            self._stop_revision = self.controller.runtime.monitoring.stop_revision(self.device_id)
            self._on_ready = ready
            # Pico's native SDK supplies session-cached input settings. Read those
            # through the ordinary worker so other UI and SSH edits are respected.
            groups = {}
            for cmd in self._draft_commands:
                groups.setdefault(cmd.channel, []).append(cmd.operation)
            self._reads = deque(groups.items())
            self._commands.clear()
            self._current = {}
            self.busy_changed.emit(True)
            self.next_step()
        except Exception as exc:
            self.failed(exc)

    def next_step(self):
        try:
            if self.controller.runtime.monitoring.stop_revision(self.device_id) != self._stop_revision:
                self.finish()
                return
            state = self.controller.states.get(self.device_id, {})
            if state.get("sessionId") != self.session_id or state.get("connectionState") not in {
                "connected",
                "busy",
            }:
                raise DeviceError("示波器会话已改变，请重新连接后采集")
            if self._reads:
                ch, names = self._reads.popleft()
                future = self.controller.runtime.sessions.submit(
                    self.device_id, "read_settings", (tuple(names), ch)
                )
                self.controller.watch(future, self.read_values, self.failed)
            elif self._draft_commands:
                for cmd in self._draft_commands:
                    current = self._current.get((cmd.channel, cmd.operation))
                    if cmd.operation == "channel" and not cmd.parameters["enabled"]:
                        if not isinstance(current, dict) or not current.get("enabled"):
                            continue
                    if not isinstance(current, dict):
                        current = {"value": current}
                    if current != cmd.parameters:
                        self._commands.append(cmd)
                self._draft_commands.clear()
                self.next_step()
            elif self._commands:
                self.controller.apply(
                    self.device_id, self._commands.popleft(), self.applied, self.failed, group_member=True
                )
            else:
                self.applied_draft = self.draft()
                ready = self._on_ready
                self.finish()
                ready()
        except Exception as exc:
            self.failed(exc)

    def read_values(self, readings):
        for reading in readings:
            self._current[(reading.channel, reading.operation)] = reading.value
        self.next_step()

    def applied(self, result):
        if result.outcome != "success":
            self.failed(DeviceError(result.error or "通道设置未确认"))
            return
        self.next_step()

    def finish(self):
        if self.running:
            self.running = False
            self.controller.end_group(self.device_id, refresh=False)
            self._on_ready = None
            self.busy_changed.emit(False)

    def failed(self, exc):
        self.error = f"采集准备失败：{exc}"
        self.finish()
        self.health.setText(self.error)
        self.controller.message.emit(self.error)

    def update_available(self, task, connected, locked):
        editing = (
            connected
            and not locked
            and not self.running
            and not task.get("running")
            and not task.get("pending")
        )
        self.scroller.setEnabled(bool(editing))
        self.health.setText(
            self.error
            or (
                "正在准备采集"
                if self.running
                else "停止监测后可编辑参数"
                if not editing and connected
                else "参数待应用 · 开始采集时生效"
                if self.channels() and self.draft() != self.applied_draft
                else "参数已应用"
                if self.channels()
                else "请至少选择一个采集通道"
            )
        )
