"""Compact spectrum draft editor and one serialized apply transaction."""

from __future__ import annotations

from typing import Any, cast

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from lab_control.domain.instruments import DeviceCommand, DeviceError
from lab_control.domain.readback import boolean_value, usable_reading
from lab_control.domain.spectrum import (
    AUTOMATIC,
    FREQUENCY,
    NUMERIC,
    SETTINGS,
    frequency_draft,
    prepare_changes,
)
from lab_control.ui.kit import Section, set_property, style_button
from lab_control.ui.pages.device_control.form import OperationEditor
from lab_control.ui.pages.monitoring.spectrum_markers import frequency_text

SECTIONS = {
    "start_frequency": "频率",
    "stop_frequency": "频率",
    "center_frequency": "频率",
    "span": "频率",
    "reference_level": "幅度",
    "attenuation": "幅度",
    "rbw": "带宽",
    "vbw": "带宽",
}


class SpectrumControls(QFrame):
    busy_changed = Signal(bool)

    def __init__(self, controller, device_id: str, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("spectrumControls")
        self.controller, self.device_id = controller, device_id
        self.mode = controller.mode
        self.editors: dict[str, OperationEditor] = {}
        self.running = False
        self.link_busy = False
        self.last_frequency = ""
        self._auto_readings: dict[str, bool | None] = {}
        self.pending: list[DeviceCommand] = []
        self.completed: list[str] = []
        self.read_pending: list[str] = []
        self.auto_running = False
        self.refresh_only = False
        self.session_id = ""
        self.verified: dict[str, Any] = {}
        self.frequency_target: dict[str, float] = {}
        self.write_failure = ("", "")
        # Card: 参数 + sync state + refresh / grouped fields / apply and auto setup.
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
        self.health = QLabel("同步中", self)
        self.health.setObjectName("spectrumHealth")
        self.health.setFixedHeight(22)
        heading.addWidget(self.health)
        heading.addStretch(1)
        self.refresh_button = QPushButton("刷新参数", self)
        style_button(self.refresh_button, "ghost", "refresh", "sm")
        self.refresh_button.setToolTip("读取仪器当前参数，保留未应用草稿；前面板修改后请点击刷新。")
        self.refresh_button.clicked.connect(self.refresh_parameters)
        heading.addWidget(self.refresh_button)
        layout.addWidget(head)
        self.scroller = QScrollArea(self)
        self.scroller.setWidgetResizable(True)
        self.scroller.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget(self.scroller)
        body.setObjectName("cardBody")
        self.body_layout = QVBoxLayout(body)
        self.body_layout.setContentsMargins(0, 0, 0, 8)
        self.body_layout.setSpacing(0)
        self.sections: dict[str, tuple[Section, QGridLayout]] = {}
        for name in ("频率", "幅度", "带宽", "其他"):
            section = Section(name, body)
            grid = QGridLayout()
            grid.setContentsMargins(0, 0, 0, 0)
            grid.setHorizontalSpacing(12)
            grid.setVerticalSpacing(12)
            grid.setColumnStretch(0, 1)
            grid.setColumnStretch(1, 1)
            section.layout_.addLayout(grid)
            section.hide()
            self.body_layout.addWidget(section)
            self.sections[name] = (section, grid)
        self.grid = self.sections["其他"][1]
        self.marker_section = Section("", body, last=True)
        self.marker_slot = self.marker_section.layout_
        self.body_layout.addWidget(self.marker_section)
        self.body_layout.addStretch()
        self.scroller.setWidget(body)
        layout.addWidget(self.scroller, 1)
        foot = QFrame(self)
        foot.setObjectName("cardFoot")
        foot_layout = QVBoxLayout(foot)
        foot_layout.setContentsMargins(14, 10, 14, 12)
        foot_layout.setSpacing(8)
        self.status = QLabel(self)
        self.status.setObjectName("inputFeedback")
        self.status.setWordWrap(True)
        foot_layout.addWidget(self.status)
        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.apply_button = QPushButton("应用修改", self)
        self.apply_button.setObjectName("primaryButton")
        self.auto_button = QPushButton("自动设置", self)
        style_button(self.auto_button, "", "wand")
        for button in (self.auto_button, self.apply_button):
            button.setProperty("buttonSize", "panel")
            buttons.addWidget(button, 1)
        foot_layout.addLayout(buttons)
        layout.addWidget(foot)
        self.apply_button.clicked.connect(self.apply)
        self.auto_button.clicked.connect(self.auto_setup)
        controller.refreshed.connect(self.refresh)
        self.refresh()

    def sync_controls(self) -> None:
        capabilities = self.controller.capabilities(self.device_id)
        by_name = {cap.name: cap for cap in capabilities.operations if cap.available and cap.writable}
        caps = {name: by_name[name] for name in SETTINGS if name in by_name}
        if caps.keys() == self.editors.keys():
            return
        # Preserve active edits; recreate only an automatic control whose host changes.
        removed = self.editors.keys() - caps.keys()
        for name, automatic in AUTOMATIC.items():
            if (name in caps) != (name in self.editors) and automatic in self.editors:
                removed.add(automatic)
        for name in removed:
            op = self.editors.pop(name)
            self.sections[SECTIONS.get(name, "其他")][1].removeWidget(op)
            op.hide()
            if name in AUTOMATIC.values():
                op.editors["value"].input.hide()
                op.editors["value"].input.setParent(op)
            op.deleteLater()
        for name, cap in caps.items():
            if name in self.editors:
                continue
            op = OperationEditor(self.controller, self.device_id, cap, 1, self, compact=True, actions=False)
            # The panel refreshes editors before applying linked automatic/manual state.
            self.controller.refreshed.disconnect(op.refresh_enabled)
            op.enable_auto_sync()
            self.editors[name] = op
            field = op.editors["value"]
            field.unit.setMinimumWidth(0)
            fit = getattr(field.unit, "fit_contents", None)
            if callable(fit):
                fit(44)
            else:
                field.unit.setFixedWidth(64)
            field.input.setMinimumWidth(36)
            field.changed.connect(self.changed)
            if name in NUMERIC:
                title = op.findChild(QLabel, "controlTitle")
                if title:
                    title.setMinimumHeight(22)
                if title and name in {"rbw", "vbw", "span"}:
                    title.setText({"rbw": "RBW", "vbw": "VBW", "span": "扫宽"}[name])
            if name in FREQUENCY:
                if isinstance(field.input, QLineEdit):
                    field.input.textEdited.connect(lambda _text, n=name: self.frequency_started(n))
                    field.input.editingFinished.connect(
                        lambda n=name: QTimer.singleShot(0, self, lambda: self.link(n))
                    )
                field.unit.currentIndexChanged.connect(
                    lambda _i, n=name: QTimer.singleShot(0, self, lambda: self.link(n))
                )
        for name, automatic in AUTOMATIC.items():
            if name in self.editors and automatic in self.editors:
                check = cast(QCheckBox, self.editors[automatic].editors["value"].input)
                check.setText("自动")
                item = cast(QVBoxLayout, self.editors[name].layout()).itemAt(0)
                assert item is not None
                heading = cast(QHBoxLayout, item.layout())
                heading.addWidget(check)
                check.show()
                # The automatic switch takes the range's place; the range stays in the tooltip.
                main = self.editors[name].editors["value"]
                main.range_hidden = True
                main.update_range_label()
                self.sections[SECTIONS.get(automatic, "其他")][1].removeWidget(self.editors[automatic])
                self.editors[automatic].hide()
        nested = {auto for name, auto in AUTOMATIC.items() if name in self.editors}
        placed: dict[str, int] = {}
        for name in caps:
            if name in nested:
                continue
            group = SECTIONS.get(name, "其他")
            index = placed.get(group, 0)
            placed[group] = index + 1
            op = self.editors[name]
            self.sections[group][1].addWidget(op, index // 2, index % 2, Qt.AlignmentFlag.AlignTop)
            op.show()
        for group, (section, _grid) in self.sections.items():
            section.setVisible(bool(placed.get(group)))

    def frequency_started(self, name: str) -> None:
        self.last_frequency = name

    def changed(self) -> None:
        if not self.link_busy and not self.running:
            self.refresh_buttons()

    def target(self) -> dict[str, Any]:
        current = self.current()
        return {
            name: (
                current[name]
                if name in current and (not op.dirty or self._automatic(name))
                else op.editors["value"].value()
            )
            for name, op in self.editors.items()
            if name in current
        }

    def _automatic(self, name: str) -> bool:
        automatic = AUTOMATIC.get(name, "")
        editor = self.editors.get(automatic)
        if editor is not None:
            return cast(QCheckBox, editor.editors["value"].input).isChecked()
        # A read-only automatic setting must be confirmed off before manual editing.
        return self._auto_readings.get(automatic, False) is not False

    def current(self) -> dict[str, Any]:
        return {
            name: op.current_value["value"]
            for name, op in self.editors.items()
            if op._reading_valid and op.current_value is not None
        }

    def link(self, changed: str) -> None:
        if self.link_busy or self.running or not all(k in self.editors for k in FREQUENCY):
            return
        op = self.editors[changed]
        field = op.editors["value"]
        if field.unit.view().isVisible() or not op.dirty:
            return
        if any(self.editors[k].editing() for k in FREQUENCY if k != changed):
            return
        self.link_busy = True
        try:
            field.finish_editing(force=True)
            values = {name: self.editors[name].editors["value"].value() for name in FREQUENCY}
            start = self.editors["start_frequency"].editors["value"].spec
            stop = self.editors["stop_frequency"].editors["value"].spec
            if start.minimum is None or stop.maximum is None:
                return
            linked = frequency_draft(values, changed, start.minimum, stop.maximum)
            adjusted = linked[changed] != values[changed]
            for name, value in linked.items():
                editor = self.editors[name]
                number = editor.editors["value"]
                factor = number.unit.currentData() or 1
                number.restore({"value": f"{value / factor:.12g}", "factor": factor})
                editor.save_draft()
            if adjusted:
                field.feedback.setText("已按有效频率范围调整")
        except DeviceError:
            pass  # Empty first-sync fields are not drafts and must never become zero.
        finally:
            self.link_busy = False
        self.refresh_buttons()

    def refresh(self) -> None:
        state = self.controller.states.get(self.device_id, {})
        frames = self.controller.runtime.sessions.readback_frames(self.device_id)
        if not self.running:
            self.sync_controls()
            for name, op in self.editors.items():
                op.refresh_enabled()
                frame = frames.get((1, name))
                if frame is None or (frame.session_id, frame.configuration_version) != (
                    state.get("sessionId"),
                    state.get("configurationVersion"),
                ):
                    continue
                token = (frame.session_id, frame.configuration_version, frame.sequence)
                if op.sync_token != token:
                    op.sync_token = token
                    op.show_reading(frame.reading)
        caps = {cap.name: cap for cap in self.controller.capabilities(self.device_id).operations}
        self._auto_readings.clear()
        for name, automatic in AUTOMATIC.items():
            cap = caps.get(automatic)
            if name in self.editors:
                if cap is not None and cap.available and cap.readable and automatic not in self.editors:
                    frame = frames.get((1, automatic))
                    value = None
                    if (
                        frame is not None
                        and frame.session_id == state.get("sessionId")
                        and frame.configuration_version == state.get("configurationVersion")
                        and usable_reading(frame.reading, cap.parameters)
                    ):
                        value = boolean_value(frame.reading.value)
                    self._auto_readings[automatic] = value
                enabled = not self._automatic(name) and self.editors[name].cap.writable
                self.editors[name].editors["value"].setEnabled(enabled and not self.running)
                if self._automatic(name) and self._auto_readings.get(automatic, True) is not None:
                    op = self.editors[name]
                    if op._reading_valid:
                        value = op.current_value["value"]
                        unit_field = op.editors["value"]
                        op.readback.setText(
                            f"自动采用：{value / (unit_field.unit.currentData() or 1):.8g} {unit_field.unit.currentText()}"
                        )
                        op.readback.show()
        for op in self.editors.values():
            field = op.editors["value"]
            spec = field.spec
            if spec.kind not in {"number", "integer"}:
                continue
            unit = field.unit.currentText()
            factor = field.unit.currentData() or 1

            def bound(value):
                if value is None:
                    return "未确认"
                return frequency_text(value) if spec.unit == "Hz" else f"{value / factor:g}"

            source = {
                "model": "型号范围",
                "instrument": "实机范围",
                "experiment": "实验限制",
                "unavailable": "未确认 / 已过期",
            }.get(spec.range_source, spec.range_source)
            text = f"{bound(spec.minimum)} ～ {bound(spec.maximum)}" + (
                "" if spec.unit == "Hz" else " " + unit
            )
            if field.range_label.text() != text:
                field.range_label.setText(text)
                field.range_label.setToolTip(f"可输入范围：{text} · {source}")
        health, reason = self.controller.runtime.monitoring.settings_status(
            self.device_id, tuple((1, k) for k in self.editors)
        )
        self.health.setText(
            "采集中 · 参数已锁定"
            if state.get("controlsLocked") and health != "同步异常"
            else "正在自动设置…"
            if self.running and self.auto_running
            else "正在读取…"
            if self.running and self.refresh_only
            else "正在应用…"
            if self.running
            else "待应用"
            if self.dirty()
            else health
        )
        self.health.setToolTip(reason)
        text = self.health.text()
        set_property(
            self.health,
            "kind",
            "err"
            if "异常" in text or "失败" in text
            else "warn"
            if text.startswith("正在") or "同步中" in text
            else "accent"
            if "待应用" in text or "锁定" in text
            else "ok",
        )
        self.refresh_buttons()

    def dirty(self) -> bool:
        return any(op.dirty and not self._automatic(name) for name, op in self.editors.items())

    def draft_has_readback(self) -> bool:
        required = {name for name, op in self.editors.items() if op.dirty and not self._automatic(name)}
        if required.intersection(FREQUENCY):
            required.update(self.editors.keys() & set(FREQUENCY))
        required.update(auto for name, auto in AUTOMATIC.items() if name in required and auto in self.editors)
        return required <= self.current().keys()

    def refresh_buttons(self) -> None:
        state = self.controller.states.get(self.device_id, {})
        locked = bool(state.get("controlsLocked"))
        self.scroller.setEnabled(not self.running)
        for op in self.editors.values():
            op.setEnabled(not locked)
        available = (
            state.get("connectionState") in {"connected", "busy"}
            and not self.running
            and not locked
            and not state.get("capabilityStale", True)
            and self.mode == self.controller.mode
            and self.device_id not in self.controller.group_devices
        )
        self.apply_button.setVisible(bool(self.editors))
        self.refresh_button.setVisible(bool(self.editors))
        self.health.setVisible(bool(self.editors))
        self.apply_button.setEnabled(available and self.dirty() and self.draft_has_readback())
        self.refresh_button.setEnabled(
            state.get("connectionState") in {"connected", "busy"}
            and not locked
            and not self.running
            and self.device_id not in self.controller.group_devices
        )
        caps = self.controller.capabilities(self.device_id)
        auto = next((c for c in caps.operations if c.name == "auto_setup"), None)
        self.auto_button.setVisible(bool(auto and auto.available and auto.writable))
        self.auto_button.setEnabled(bool(available and auto and auto.available and auto.writable))
        self.auto_button.setToolTip(
            "执行仪器 Auto Tune" if auto and auto.available else "当前型号未核实整机 Auto 接口"
        )

    def apply(self) -> None:
        try:
            for op in self.editors.values():
                if op.dirty:
                    op.editors["value"].finish_editing(force=True)
            if self.last_frequency:
                self.link(self.last_frequency)
            target = self.target()
            current = self.current()
            if not self.draft_has_readback():
                raise DeviceError("等待有效读回后再应用")
            self.pending = prepare_changes(current, target, self.controller.capabilities(self.device_id))
            if self.pending:
                self.begin(False)
                if any(cmd.operation in FREQUENCY for cmd in self.pending):
                    self.frequency_target = {name: target[name] for name in FREQUENCY}
                self.next_command()
        except Exception as exc:
            self.status.setText(str(exc))

    def refresh_parameters(self) -> None:
        try:
            self.begin(False)
            self.refresh_only = True
            self.read_pending = list(self.editors)
            self.read_next()
        except DeviceError as exc:
            self.status.setText(str(exc))

    def auto_setup(self) -> None:
        if self.running:
            return
        if (
            self.dirty()
            and QMessageBox.question(
                self,
                "替换未应用修改",
                "自动设置成功后，将用仪器实际结果替换当前未应用修改。继续？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        self.pending = [DeviceCommand("auto_setup")]
        try:
            self.begin(True)
            self.next_command()
        except DeviceError as exc:
            self.status.setText(str(exc))

    def begin(self, auto: bool) -> None:
        self.controller.ensure_applicable(self.device_id)
        self.controller.begin_group(self.device_id)
        self.session_id = self.controller.states[self.device_id]["sessionId"]
        self.completed, self.verified = [], {}
        self.frequency_target = {}
        self.write_failure = ("", "")
        self.auto_running, self.running = auto, True
        self.refresh_only = False
        self.scroller.setEnabled(False)
        self.busy_changed.emit(True)
        self.refresh()

    def valid_session(self) -> bool:
        state: dict[str, Any] = next(
            (
                s
                for s in self.controller.runtime.sessions.snapshots()
                if s["stableDeviceId"] == self.device_id
            ),
            {},
        )
        return state.get("sessionId") == self.session_id

    def next_command(self) -> None:
        if not self.valid_session():
            self.finish("会话已改变，停止后续操作；草稿已保留")
            return
        if not self.pending:
            # Verify every setting that was readable, including all writes, without
            # requiring an unrelated unsupported readback to finish this transaction.
            self.read_pending = (
                list(self.editors)
                if self.auto_running
                else list(dict.fromkeys([*self.current(), *self.completed]))
            )
            self.read_next()
            return
        command = self.pending[0]
        try:
            if command.operation != "auto_setup":
                op = self.editors[command.operation]
                op.refresh_enabled()
                if not op.cap.available or not op.cap.writable:
                    self.finish("参数可写范围已变化，请核对当前可用控件", failed=command.operation)
                    return
                actual = op.parameters()
                if actual != command.parameters:
                    self.finish("范围已变化，请核对修正后的草稿，再次应用")
                    return
            self.controller.apply(
                self.device_id,
                command,
                self.applied,
                lambda exc: self.write_failed(command.operation, str(exc)),
                group_member=True,
            )
        except Exception as exc:
            self.write_failed(command.operation, str(exc))

    def applied(self, result) -> None:
        if not self.valid_session():
            self.finish("会话已改变，已丢弃旧结果")
        elif result.outcome != "success":
            self.write_failed(self.pending[0].operation, result.error or result.outcome)
        else:
            self.completed.append(self.pending.pop(0).operation)
            self.next_command()

    def write_failed(self, operation: str, reason: str) -> None:
        if self.frequency_target and self.valid_session():
            self.write_failure = (operation, reason)
            self.read_pending = list(FREQUENCY)
            self.read_next()
        else:
            self.finish(reason, failed=operation)

    def read_next(self) -> None:
        if not self.valid_session():
            self.finish("会话已改变，已丢弃旧结果")
            return
        if not self.read_pending:
            if self.frequency_target:
                actual = {}
                for name in FREQUENCY:
                    reading = self.verified.get(name)
                    if reading is not None:
                        value = reading.value
                        actual[name] = float(value["value"] if isinstance(value, dict) else value)
                failed, reason = self.write_failure
                if failed or any(
                    name not in actual or abs(actual[name] - target) > 0.5
                    for name, target in self.frequency_target.items()
                ):
                    values = "、".join(
                        f"{label} {frequency_text(actual[k]) if k in actual else '未读回'}"
                        for k, label in zip(FREQUENCY, ("起始", "终止", "中心", "扫宽"), strict=True)
                    )
                    self.finish(f"{reason or '目标与读回不一致'} · 实际读回：{values}", failed=failed)
                    return
            if self.auto_running:
                for op in self.editors.values():
                    op.dirty = False
                    self.controller.runtime.repository.save_workspace(op.key, None)
            self.finish(
                "参数已刷新 · 草稿已保留"
                if self.refresh_only
                else "自动设置完成 · 已核对读回"
                if self.auto_running
                else "修改已应用 · 已核对读回"
            )
            return
        names, self.read_pending = self.read_pending[:4], self.read_pending[4:]
        self.controller.watch(
            self.controller.runtime.sessions.submit(self.device_id, "read_settings", (tuple(names), 1)),
            self.read_done,
            lambda exc: self.finish(
                (self.write_failure[1] + " · " if self.write_failure[0] else "")
                + ("参数读取失败：" if self.refresh_only else "设置已执行，读回未确认：")
                + str(exc),
                failed=self.write_failure[0],
            ),
        )

    def read_done(self, readings) -> None:
        from lab_control.domain.readback import usable_reading

        for reading in readings:
            op = self.editors[reading.operation]
            if not usable_reading(reading, op.cap.parameters):
                if reading.operation in self.frequency_target:
                    continue
                self.finish("读回未确认；草稿已保留")
                return
            self.verified[reading.operation] = reading
        self.read_next()

    def finish(self, text: str, *, failed: str = "") -> None:
        self.running = False
        self.scroller.setEnabled(True)
        self.controller.end_group(self.device_id)
        self.busy_changed.emit(False)
        if failed:
            done = "、".join(self.completed) or "无"
            remaining = "、".join(c.operation for c in self.pending if c.operation != failed) or "无"
            text += f" · 已完成：{done} · 失败：{failed} · 未执行：{remaining}"
        self.status.setText(text)
        self.refresh()
