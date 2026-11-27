"""Compact power drafts; explicit control/readback runs on the shared worker."""

from typing import Any, cast

import shiboken6
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from lab_control.application.settings_sync import SettingsSync
from lab_control.domain.instruments import DeviceError
from lab_control.domain.readback import usable_reading
from lab_control.ui.kit import Dot, Pill, style_button
from lab_control.ui.kit.numeric import ReadingLabel, number_text
from lab_control.ui.pages.device_control.power_supply import PowerOutput
from lab_control.ui.pages.device_control.signal_source import ParameterGroup, newer_token
from lab_control.ui.pages.monitoring.frame import show_sync_health
from lab_control.ui.pages.monitoring.power_plot import CHANNEL_COLORS


class PowerMonitorChannel(ParameterGroup):
    def __init__(self, owner, channel: int):
        self.owner = owner
        schema = owner.controller.capabilities(owner.device_id, channel)
        writable = {
            c.name
            for c in owner.controller.declared_capabilities(owner.device_id, channel).operations
            if c.writable
        }
        caps = [
            c
            for name in ("voltage", "current")
            for c in schema.operations
            if c.name == name and name in writable
        ]
        super().__init__(owner.controller, owner.device_id, channel, caps, columns=2, parent=owner)
        self.setObjectName("powerMonitorChannel")
        layout = cast(QVBoxLayout, self.layout())
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(5)
        title = QLabel(f"CH {channel}")
        title.setObjectName("powerChannelTitle")
        heading = QHBoxLayout()
        heading.setSpacing(6)
        heading.addWidget(Dot(CHANNEL_COLORS[(channel - 1) % 4], 7, box=10))
        heading.addWidget(title)
        heading.addStretch()
        self.footer_layout.removeWidget(self.count)
        self.footer_layout.removeWidget(self.apply_button)
        heading.addWidget(self.count)
        heading.addWidget(self.apply_button)
        layout.insertLayout(0, heading)
        self.measurements: dict[str, QLabel] = {}
        actual = QHBoxLayout()
        for name in ("voltage", "current"):
            if name not in self.editors and not any(
                c.name == "measured_" + name and c.readable for c in schema.operations
            ):
                continue
            label = ReadingLabel("实测 — " + ("V" if name == "voltage" else "A"))
            label.setObjectName("powerLiveValue")
            # Equal readout columns must not resize when a value gains digits.
            label.setMinimumWidth(0)
            label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
            label.setWordWrap(False)
            actual.addWidget(label, 1)
            self.measurements[name] = label
            if name not in self.editors:
                continue
            editor = self.editors[name]
            field_title = editor.findChild(QLabel, "controlTitle")
            if field_title:
                field_title.setText("电压设置" if name == "voltage" else "电流限值")
            field = editor.editors["value"]
            field.changed.connect(self.refresh)
            field.simplified = True
            field.update_range_label()
            cast(QVBoxLayout, editor.layout()).setSpacing(3)
            field.input.setMinimumWidth(0)
            field.unit.setMinimumWidth(0)
            field.unit.fit_contents(54)
            field.unit.setFixedWidth(54)
        layout.insertLayout(1, actual)
        self.apply_button.setText("应用")
        self.apply_button.setVisible(bool(self.editors))
        self.apply_button.setProperty("kitSize", "sm")
        self.apply_button.setFixedWidth(64)
        self.fields.setSpacing(8)
        self.output = None
        if not owner.master and any(c.name == "output" for c in schema.operations):
            self.output = MonitoredPowerOutput(owner, "output", channel)
            layout.insertWidget(2, self.output)

    def reflow_fields(self) -> None:
        self.fields.setColumnStretch(0, 1)
        self.fields.setColumnStretch(1, 1)
        self.visible_columns = 2

    def resizeEvent(self, event) -> None:  # noqa: N802
        QWidget.resizeEvent(self, event)
        self.update_row_heights()

    def refresh(self) -> None:
        super().refresh()
        if self.count.text() == "同步已暂停" and self.owner.current_settings(self.channel):
            self.count.setText("已读设定")
            self.count.setToolTip("沿用最近已核对的设定值；前面板修改后请刷新参数。")

    def finish(self, reason: str, *, failed: str | None = None) -> None:
        if self.running and not failed and not self.pending:
            # Keep the control group held until all affected channel settings are current.
            self.owner.read_parameters(
                lambda error: super(PowerMonitorChannel, self).finish(
                    error or reason, failed="读回" if error else None
                ),
                own_group=False,
            )
        else:
            super().finish(reason, failed=failed)


class MonitoredPowerOutput(PowerOutput):
    def __init__(self, owner, operation, channel):
        self.owner = owner
        self.group_held = False
        super().__init__(owner.controller, owner.device_id, operation, channel, owner, allow_cached=True)

    def submit(self, command) -> None:
        self.controller.begin_group(self.device_id)
        self.group_held = True
        self.controller.apply(self.device_id, command, self.completed, self.failed, group_member=True)

    def release_group(self) -> None:
        if self.group_held:
            self.group_held = False
            self.controller.end_group(self.device_id, refresh=False)

    def completed(self, result) -> None:
        if not shiboken6.isValid(self):
            return
        if result.outcome != "success":
            self.failed(DeviceError(result.error or result.outcome))
            return
        self.owner.read_parameters(self.read_complete, own_group=False)

    def read_complete(self, error: str) -> None:
        self.release_group()
        if not shiboken6.isValid(self):
            return
        self.busy = False
        if error:
            self.controller.message.emit(error)
        self.refresh()

    def failed(self, error: Exception) -> None:
        self.release_group()
        super().failed(error)


class PowerControls(QWidget):
    busy_changed = Signal(bool)

    def __init__(self, controller, device_id: str, parent=None):
        super().__init__(parent)
        self.controller, self.device_id = controller, device_id
        self.panels: dict[int, PowerMonitorChannel] = {}
        self.running = False
        self.read_queue: list[tuple[int, tuple[str, ...]]] = []
        self.expected_read: tuple[int, tuple[str, ...]] = (1, ())
        self.callback = None
        self.owns_group = False
        self.session_id = ""
        schema = controller.capabilities(device_id)
        self.master = any(
            c.name == "all_outputs" and c.writable
            for c in controller.declared_capabilities(device_id).operations
        )
        # Parameter card: title and refresh, whole-device output, one panel per channel.
        self.setObjectName("paramCard")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        head = QFrame(self)
        head.setObjectName("cardHead")
        head.setFixedHeight(52)
        header = QHBoxLayout(head)
        header.setContentsMargins(16, 0, 10, 0)
        header.setSpacing(8)
        title = QLabel("通道设置")
        title.setObjectName("cardTitle")
        header.addWidget(title)
        self.health = Pill("待同步", "warn", dot=True)
        header.addWidget(self.health)
        header.addStretch(1)
        self.refresh_button = QPushButton("刷新参数")
        style_button(self.refresh_button, "ghost", "refresh", "sm")
        self.refresh_button.clicked.connect(lambda: self.read_parameters())
        header.addWidget(self.refresh_button)
        layout.addWidget(head)
        self.output = MonitoredPowerOutput(self, "all_outputs", 1) if self.master else None
        if self.output:
            self.output.setContentsMargins(16, 10, 16, 0)
            layout.addWidget(self.output)
        self.scroller = QScrollArea()
        self.scroller.setWidgetResizable(True)
        body = QWidget()
        body.setObjectName("cardBody")
        rows = QVBoxLayout(body)
        rows.setContentsMargins(12, 12, 12, 12)
        rows.setSpacing(10)
        for channel in schema.channels:
            panel = PowerMonitorChannel(self, channel)
            self.panels[channel] = panel
            rows.addWidget(panel)
        rows.addStretch()
        self.scroller.setWidget(body)
        layout.addWidget(self.scroller, 1)
        self.status = QLabel()
        self.status.setObjectName("inputFeedback")
        self.status.setWordWrap(True)
        self.status.hide()
        self.status.setContentsMargins(16, 6, 16, 10)
        layout.addWidget(self.status)
        controller.settings_changed.connect(self.sync)
        controller.refreshed.connect(self.refresh)
        self.sync(device_id)

    def current_settings(self, channel: int) -> bool:
        state = self.controller.states.get(self.device_id, {})
        frames = self.controller.runtime.sessions.readback_frames(self.device_id)
        schema = self.controller.capabilities(self.device_id, channel)
        return all(
            SettingsSync.current(frames.get((channel, cap.name)), state)
            and usable_reading(frames[(channel, cap.name)].reading, cap.parameters)
            for cap in schema.operations
            if cap.name in {"voltage", "current"} and cap.available and cap.readable
        )

    def sync(self, device_id: str) -> None:
        if device_id != self.device_id:
            return
        frames = self.controller.runtime.sessions.readback_frames(device_id)
        state = self.controller.states.get(device_id, {})
        for channel, panel in self.panels.items():
            for name, editor in panel.editors.items():
                frame = frames.get((channel, name))
                if not frame or not SettingsSync.current(frame, state):
                    editor._reading_valid = False
                    continue
                token = (frame.session_id, frame.configuration_version, frame.sequence)
                if newer_token(token, editor.sync_token):
                    editor.sync_token = token
                    editor.show_reading(frame.reading)
            panel.refresh()

    def refresh(self) -> None:
        state = self.controller.states.get(self.device_id, {})
        locked = bool(state.get("controlsLocked"))
        dirty = any(e.dirty and e.cap.writable for p in self.panels.values() for e in p.editors.values())
        keys = tuple(key for panel in self.panels.values() for key in panel.sync_keys())
        health, reason = self.controller.runtime.monitoring.settings_status(self.device_id, keys)
        self.health.setToolTip(reason)
        if health == "同步异常":
            show_sync_health(self.health, health)
        elif locked:
            self.health.set("采集中 · 参数已锁定", "warn", icon="lock", dot=False)
        elif self.running:
            self.health.set("正在读取…", "warn", icon="", dot=True, live=True)
        elif dirty:
            self.health.set("待应用", "accent", icon="", dot=True)
        else:
            show_sync_health(self.health, health)
        self.refresh_button.setEnabled(
            state.get("connectionState") in {"connected", "busy"}
            and not locked
            and not state.get("controlPending")
            and self.device_id not in self.controller.group_devices
        )

    def read_parameters(self, callback=None, *, own_group=True) -> None:
        if self.running:
            return
        self.owns_group = False
        try:
            self.controller.ensure_applicable(self.device_id)
            state = self.controller.states.get(self.device_id, {})
            if state.get("connectionState") not in {"connected", "busy"}:
                raise DeviceError("设备已断开", "disconnected")
            if own_group:
                if self.device_id in self.controller.group_devices:
                    raise DeviceError("请等待当前参数操作完成", "task_busy")
                self.controller.begin_group(self.device_id)
            self.owns_group = own_group
            self.callback = callback
            self.session_id = self.controller.states[self.device_id]["sessionId"]
            self.read_queue = []
            for channel in self.panels:
                schema = self.controller.capabilities(self.device_id, channel)
                names = tuple(
                    c.name
                    for c in schema.operations
                    if c.available
                    and c.readable
                    and (
                        c.name in {"voltage", "current"}
                        or c.name == "output"
                        or (c.name == "all_outputs" and channel == 1)
                    )
                )
                self.read_queue.append((channel, names))
            self.running = True
            self.busy_changed.emit(True)
            self.status.setText("正在核对设定值…")
            self.status.show()
            self.read_next()
        except Exception as exc:
            if self.owns_group:
                self.controller.end_group(self.device_id, refresh=False)
            self.running = False
            self.callback = None
            self.busy_changed.emit(False)
            self.status.setText(str(exc))
            self.status.show()
            if callback:
                callback(str(exc))

    def read_next(self) -> None:
        state: dict[str, Any] = next(
            (
                s
                for s in self.controller.runtime.sessions.snapshots()
                if s["stableDeviceId"] == self.device_id
            ),
            {},
        )
        if state.get("sessionId") != self.session_id:
            self.finish("会话已改变，参数核对已停止")
        elif not self.read_queue:
            self.finish("")
        else:
            channel, names = self.read_queue.pop(0)
            self.expected_read = (channel, names)
            try:
                self.controller.watch(
                    self.controller.runtime.sessions.submit(
                        self.device_id, "read_settings", (names, channel)
                    ),
                    self.read_done,
                    lambda exc: self.finish(str(exc)),
                )
            except Exception as exc:
                self.finish(str(exc))

    def read_done(self, readings) -> None:
        channel, names = self.expected_read
        if len(readings) != len(names) or {(r.channel, r.operation) for r in readings} != {
            (channel, name) for name in names
        }:
            self.finish("参数读回不完整，监测已停止")
            return
        for reading in readings:
            cap = self.controller.capabilities(self.device_id, reading.channel).get(reading.operation)
            if not usable_reading(reading, cap.parameters):
                self.finish(reading.error or "参数读回无效，监测已停止")
                return
        self.read_next()

    def finish(self, error: str) -> None:
        self.running = False
        if error:
            self.controller.watch(self.controller.runtime.monitoring.stop(self.device_id))
        if self.owns_group:
            self.controller.end_group(self.device_id, refresh=False)
        self.sync(self.device_id)
        self.status.setText(error)
        self.status.setVisible(bool(error))
        callback, self.callback = self.callback, None
        if callback:
            callback(error)
        self.busy_changed.emit(False)
        self.refresh()

    def show_measurements(self, frame, channels: tuple[int, ...], *, paused=False) -> None:
        values = {(m.channel, m.quantity): m for m in frame.batch.measurements} if frame else {}
        for channel, panel in self.panels.items():
            for quantity, label in panel.measurements.items():
                m = values.get((channel, quantity))
                value = f"{number_text(m.values[0])} {m.unit}" if m is not None else "—"
                text = f"实测 {value}" + (
                    " · 未采集" if channel not in channels else " · 已暂停" if paused and frame else ""
                )
                label.setText(text)
                label.setToolTip(frame.batch.timestamp if frame and m else "等待有效采集帧")
