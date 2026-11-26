"""Four-setting optical spectrometer draft editor."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from lab_control.domain.instruments import DeviceCommand, DeviceError
from lab_control.domain.readback import usable_reading
from lab_control.domain.spectrum import OPTICAL_SETTINGS, prepare_optical_changes
from lab_control.ui.kit import Pill, Section, set_property, style_button
from lab_control.ui.pages.device_control.form import OperationEditor
from lab_control.ui.pages.monitoring.frame import show_sync_health


class OpticalSpectrumControls(QFrame):
    """Keep instrument settings separate from the software sampling interval."""

    busy_changed = Signal(bool)

    def __init__(self, controller, device_id: str, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("spectrumControls")
        self.controller, self.device_id, self.mode = controller, device_id, "general"
        self.editors: dict[str, OperationEditor] = {}
        self.running = False
        self.refresh_only = False
        self.pending: list[DeviceCommand] = []
        self.completed: list[str] = []
        self.read_pending: list[str] = []
        self.session_id = ""
        self.verified: dict[str, Any] = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        head = QFrame(self)
        head.setObjectName("cardHead")
        head.setFixedHeight(52)
        heading = QHBoxLayout(head)
        heading.setContentsMargins(16, 0, 10, 0)
        heading.setSpacing(8)
        title = QLabel("参数", head)
        title.setObjectName("cardTitle")
        heading.addWidget(title)
        self.health = Pill("同步中", "warn", dot=True)
        heading.addWidget(self.health)
        heading.addStretch(1)
        self.refresh_button = QPushButton("刷新参数", head)
        style_button(self.refresh_button, "ghost", "refresh", "sm")
        self.refresh_button.setToolTip("读取仪器当前四项光学参数，保留未应用草稿。")
        self.refresh_button.clicked.connect(self.refresh_parameters)
        heading.addWidget(self.refresh_button)
        layout.addWidget(head)
        self.scroller = QScrollArea(self)
        self.scroller.setWidgetResizable(True)
        self.scroller.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget(self.scroller)
        body.setObjectName("cardBody")
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 8)
        body_layout.setSpacing(0)
        section = Section("光学参数", body, last=True)
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(12)
        section.layout_.addLayout(grid)
        body_layout.addWidget(section)
        body_layout.addStretch(1)
        caps = {cap.name: cap for cap in controller.capabilities(device_id).operations}
        labels = {
            "start_wavelength": "起始波长",
            "stop_wavelength": "结束波长",
            "rbw": "分辨率 RBW",
            "sensitivity": "灵敏度",
        }
        declared = {cap.name: cap for cap in controller.declared_capabilities(device_id).operations}
        self.setting_names = tuple(
            name for name in OPTICAL_SETTINGS if name in declared and declared[name].readable
        )
        for name in self.setting_names:
            cap = caps.get(name)
            if cap is None or not declared[name].writable:
                continue
            op = OperationEditor(
                controller, device_id, cap, 1, body, compact=True, actions=False, workspace_mode=self.mode
            )
            index = len(self.editors)
            self.editors[name] = op
            field_title = op.findChild(QLabel, "controlTitle")
            if field_title:
                field_title.setText(labels[name])
                field_title.setMinimumHeight(20)
            field = op.editors.get("value")
            if field:
                field.input.setMinimumWidth(0)
                field.unit.setMinimumWidth(0)
                fit = getattr(field.unit, "fit_contents", None)
                if callable(fit):
                    fit(44)
                field.range_label.setMinimumHeight(18)
            op.enable_auto_sync()
            if field:
                field.changed.connect(self.refresh_buttons)
            grid.addWidget(op, index // 2, index % 2, Qt.AlignmentFlag.AlignTop)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        self.scroller.setWidget(body)
        layout.addWidget(self.scroller, 1)
        foot = QFrame(self)
        foot.setObjectName("cardFoot")
        foot_layout = QVBoxLayout(foot)
        foot_layout.setContentsMargins(14, 12, 14, 12)
        foot_layout.setSpacing(8)
        self.status = QLabel(foot)
        self.status.setObjectName("inputFeedback")
        self.status.setWordWrap(True)
        self.status.hide()
        foot_layout.addWidget(self.status)
        buttons = QHBoxLayout()
        self.apply_button = QPushButton("应用修改", foot)
        self.apply_button.setVisible(bool(self.editors))
        self.apply_button.setObjectName("primaryButton")
        self.apply_button.setProperty("buttonSize", "panel")
        self.apply_button.clicked.connect(self.apply)
        buttons.addWidget(self.apply_button)
        foot_layout.addLayout(buttons)
        layout.addWidget(foot)
        controller.refreshed.connect(self.refresh)
        self.refresh()

    def current(self) -> dict[str, Any]:
        values = {
            name: op.current_value["value"]
            for name, op in self.editors.items()
            if op._reading_valid and isinstance(op.current_value, dict) and "value" in op.current_value
        }

        state = self.controller.states.get(self.device_id, {})
        caps = {cap.name: cap for cap in self.controller.capabilities(self.device_id).operations}
        frames = self.controller.runtime.sessions.readback_frames(self.device_id)
        for name in self.setting_names:
            if name in self.editors:
                continue
            frame = frames.get((1, name))
            cap = caps.get(name)
            if (
                frame
                and cap
                and frame.session_id == state.get("sessionId")
                and frame.configuration_version == state.get("configurationVersion")
                and usable_reading(frame.reading, cap.parameters)
            ):
                raw = frame.reading.value
                values[name] = raw.get("value") if isinstance(raw, dict) else raw
        return values

    def target(self) -> dict[str, Any]:
        return {name: op.editors["value"].value() for name, op in self.editors.items()}

    def _valid_session(self) -> bool:
        state = self.controller.states.get(self.device_id, {})
        return state.get("sessionId") == self.session_id

    def apply(self) -> None:
        try:
            for op in self.editors.values():
                op.editors["value"].finish_editing(force=True)
            current = self.current()
            target = {**current, **self.target()}
            if set(current) != set(OPTICAL_SETTINGS):
                raise DeviceError("等待四项有效读回后再应用")
            self.pending = prepare_optical_changes(
                current, target, self.controller.capabilities(self.device_id)
            )
            caps = self.controller.capabilities(self.device_id)
            if any(
                cmd.operation not in self.editors
                or not caps.get(cmd.operation).available
                or not caps.get(cmd.operation).writable
                for cmd in self.pending
            ):
                raise DeviceError("当前仪器状态不允许修改所选参数")
            if self.pending:
                self._begin(False)
                self._next_command()
            else:
                self.status.setText("参数未变化")
                self.status.show()
        except Exception as exc:
            self.status.setText(str(exc))
            self.status.show()

    def refresh_parameters(self) -> None:
        try:
            self._begin(False)
            self.refresh_only = True
            self.read_pending = list(self.setting_names)
            self._read_next()
        except Exception as exc:
            self.status.setText(str(exc))
            self.status.show()

    def _begin(self, _auto: bool) -> None:
        self.controller.ensure_applicable(self.device_id, workspace_mode=self.mode)
        self.controller.begin_group(self.device_id)
        self.session_id = self.controller.states[self.device_id]["sessionId"]
        self.completed, self.verified = [], {}
        self.running = True
        self.refresh_only = False
        self.scroller.setEnabled(False)
        self.busy_changed.emit(True)
        self.refresh()

    def _next_command(self) -> None:
        if not self._valid_session():
            self.finish("会话已改变，停止后续操作；草稿已保留")
            return
        if not self.pending:
            self.read_pending = list(self.setting_names)
            self._read_next()
            return
        command = self.pending[0]
        try:
            op = self.editors[command.operation]
            if op.parameters() != command.parameters:
                self.finish("范围已变化，请核对修正后的草稿，再次应用")
                return
            self.controller.apply(
                self.device_id,
                command,
                self._applied,
                lambda exc: self.finish(str(exc), failed=command.operation),
                group_member=True,
                workspace_mode=self.mode,
            )
        except Exception as exc:
            self.finish(str(exc), failed=command.operation)

    def _applied(self, result) -> None:
        if not self._valid_session():
            self.finish("会话已改变，已丢弃旧结果")
        elif result.outcome != "success":
            self.finish(result.error or result.outcome, failed=self.pending[0].operation)
        else:
            self.completed.append(self.pending.pop(0).operation)
            self._next_command()

    def _read_next(self) -> None:
        if not self._valid_session():
            self.finish("会话已改变，已丢弃旧结果")
            return
        if not self.read_pending:
            self.finish("参数已刷新 · 草稿已保留" if self.refresh_only else "修改已应用 · 已核对读回")
            return
        names, self.read_pending = self.read_pending[:4], self.read_pending[4:]

        def failed(exc):
            self.finish(("参数读取失败：" if self.refresh_only else "设置已执行，读回未确认：") + str(exc))

        try:
            self.controller.watch(
                self.controller.runtime.sessions.submit(self.device_id, "read_settings", (tuple(names), 1)),
                lambda readings: self._read_done(names, readings),
                failed,
            )
        except Exception as exc:
            failed(exc)

    def _read_done(self, names, readings) -> None:
        if not self._valid_session():
            self.finish("会话已改变，已丢弃旧结果")
            return
        if len(readings) != len(names) or {r.operation for r in readings} != set(names):
            self.finish("参数读回不完整；草稿已保留")
            return
        caps = self.controller.capabilities(self.device_id)
        for reading in readings:
            if not usable_reading(reading, caps.get(reading.operation).parameters):
                self.finish("读回未确认；草稿已保留")
                return
            self.verified[reading.operation] = reading
        self._read_next()

    def finish(self, text: str, *, failed: str = "") -> None:
        self.running = False
        self.scroller.setEnabled(True)
        self.controller.end_group(self.device_id)
        self.busy_changed.emit(False)
        if failed:
            done = "、".join(self.completed) or "无"
            rest = (
                "、".join(command.operation for command in self.pending if command.operation != failed)
                or "无"
            )
            text += f" · 已完成：{done} · 失败：{failed} · 未执行：{rest}"
        self.status.setText(text)
        self.status.setVisible(bool(text))
        self.refresh()

    def refresh_buttons(self) -> None:
        state = self.controller.states.get(self.device_id, {})
        locked = bool(state.get("controlsLocked"))
        set_property(self, "locked", locked)
        for op in self.editors.values():
            op.setEnabled(not locked)
        available = (
            state.get("connectionState") in {"connected", "busy"}
            and not self.running
            and not locked
            and not state.get("capabilityStale", True)
            and self.device_id not in self.controller.group_devices
        )
        caps = self.controller.capabilities(self.device_id)
        dirty = [name for name, op in self.editors.items() if op.dirty]
        self.apply_button.setEnabled(
            available
            and set(self.current()) == set(OPTICAL_SETTINGS)
            and bool(dirty)
            and all(caps.get(name).available and caps.get(name).writable for name in dirty)
        )
        self.refresh_button.setEnabled(available and bool(self.setting_names))
        self.busy_changed.emit(self.running)

    def refresh(self) -> None:
        state = self.controller.states.get(self.device_id, {})
        if not self.running:
            frames = self.controller.runtime.sessions.readback_frames(self.device_id)
            for name, op in self.editors.items():
                frame = frames.get((1, name))
                if frame is None or (frame.session_id, frame.configuration_version) != (
                    state.get("sessionId"),
                    state.get("configurationVersion"),
                ):
                    op._reading_valid = False
                    op.sync_token = None
                    continue
                token = (frame.session_id, frame.configuration_version, frame.sequence)
                if op.sync_token != token:
                    op.sync_token = token
                    op.show_reading(frame.reading)
        health, reason = self.controller.runtime.monitoring.settings_status(
            self.device_id, tuple((1, name) for name in self.setting_names)
        )
        dirty = any(op.dirty for op in self.editors.values())
        if health == "同步异常":
            show_sync_health(self.health, health)
        elif state.get("controlsLocked"):
            self.health.set("采集中 · 参数已锁定", "warn", icon="lock", dot=False)
        elif self.running:
            self.health.set(
                "正在读取…" if self.refresh_only else "正在应用…", "warn", icon="", dot=True, live=True
            )
        elif dirty:
            self.health.set("待应用", "accent", icon="", dot=True)
        else:
            show_sync_health(self.health, health)
        self.health.setToolTip(reason)
        if self.running and not self.refresh_only:
            self.status.setText("正在应用…")
            self.status.show()
        self.refresh_buttons()
