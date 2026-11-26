"""Oscilloscope workspace: passive monitoring and explicit instrument gestures."""

from dataclasses import dataclass, replace

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QSplitter,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from lab_control.application.config import config_from_record
from lab_control.application.controls import DataSource, data_sources
from lab_control.application.monitoring_state import MonitorFrame
from lab_control.domain.frames import immutable_batch
from lab_control.domain.instruments import AcquisitionRequest
from lab_control.domain.oscilloscope import ScopeDisplay, engineering
from lab_control.domain.waveform_coverage import mark_coverage
from lab_control.instruments.scope_single import SingleCapture
from lab_control.ui.controller import state_label
from lab_control.ui.kit import Dot, EmptyState, IconButton, PageHeader, ReadoutStrip, icons
from lab_control.ui.pages.monitoring.connection import refresh_reconnect_button
from lab_control.ui.pages.monitoring.oscilloscope_controls import OscilloscopeControls, ScopeSlider
from lab_control.ui.pages.monitoring.oscilloscope_plot import OscilloscopePlot
from lab_control.ui.pages.monitoring.pico_scope_controls import PicoScopeControls
from lab_control.ui.pages.monitoring.recording_state import (
    RecordingButton,
    recording_phase,
    recording_save_notice,
    update_recording_frame,
)
from lab_control.ui.pages.monitoring.timing import MonitoringInterval, actual_interval_text
from lab_control.ui.styles import tokens as tk
from lab_control.ui.widgets.device_selector import DeviceSelector
from lab_control.ui.widgets.plot_export import PlotExport
from lab_control.ui.widgets.popup_surface import WorkspaceMenu


@dataclass(frozen=True)
class TagDrag:
    device: str
    session: str
    version: int
    channel: int
    operation: str
    display: ScopeDisplay
    field: ScopeSlider


class OscilloscopeMonitoringPage(QWidget):
    def __init__(self, controller, add_card=None, parent=None) -> None:
        super().__init__(parent)
        self.controller = controller
        self.device_id, self.mode = "", controller.mode
        self.pico_drafts: dict[tuple[str, str], dict] = {}
        self.controls: OscilloscopeControls | PicoScopeControls | None = None
        self.source: DataSource | None = None
        self.memory_busy = False
        self.tag_drag: TagDrag | None = None
        self.plot: OscilloscopePlot
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(14)
        header = PageHeader(self)
        self.selector = DeviceSelector(controller, "oscilloscope", self, compact=True)
        header.add_left(self.selector, 1)
        self.connection = QLabel(self)
        self.connection.setObjectName("sourceConnectionState")
        self.connection.hide()
        self.single_job: SingleCapture | None = None
        self.single_notice = ""
        self.pausing_for_single: object | None = None
        self.frozen_at = 0.0
        self.start_button = QToolButton()
        self.start_button.setObjectName("runButton")
        self.start_button.setProperty("buttonSize", "compact")
        self.start_button.setText("开始监测")
        self.start_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.start_button.setIcon(icons.icon("play", tk.TEXT))
        self.start_button.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        actions = WorkspaceMenu(self.start_button)
        self.single_action = actions.addAction("单次采集", self.request_single)
        self.single_action.setToolTip("Single：仪器采集一帧后停止，软件读取这一帧")
        self.cancel_action = actions.addAction("取消本次采集", self.cancel_single)
        self.memory_action = actions.addAction("读取完整存储波形", self.read_memory)
        self.memory_action.setVisible(False)
        self.start_button.setMenu(actions)
        self.start_button.clicked.connect(self.toggle_monitoring)
        self.single_action.setIcon(icons.icon("single", tk.TEXT))
        self.timing = MonitoringInterval(controller, self)
        header.add_right(self.timing)
        header.add_right(self.start_button)
        self.record_button = RecordingButton(self)
        self.record_button.clicked.connect(self.record)
        header.add_right(self.record_button)
        layout.addWidget(header)
        self.empty = EmptyState("未选择示波器", "请先在首页连接示波器，再从上方选择设备。", "scope")
        layout.addWidget(self.empty, 1)
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        chart = self.chart = QFrame()
        chart.setObjectName("spectrumChart")
        chart.setMinimumWidth(280)
        chart_layout = QVBoxLayout(chart)
        chart_layout.setContentsMargins(0, 0, 0, 0)
        chart_layout.setSpacing(0)
        head = QFrame(chart)
        head.setObjectName("cardHead")
        tools_row = QHBoxLayout(head)
        tools_row.setContentsMargins(0, 0, 10, 0)
        tools_row.setSpacing(4)
        self.readouts = ReadoutStrip(
            {
                "timebase": "时基",
                "rate": "采样率",
                "points": "记录深度",
                "actual_points": "实际点数",
                "trigger": "触发",
            },
            highlight=("trigger",),
            embedded=True,
        )
        tools_row.addWidget(self.readouts, 1)
        self.fit_button = IconButton("updown", "恢复视图（按当前时基、通道档位和偏移显示）")
        self.fit_button.clicked.connect(lambda: self.plot.fit_current())
        tools_row.addWidget(self.fit_button)
        self.save_button = IconButton("download", "保存当前帧")
        self.save_button.clicked.connect(lambda: self.save())
        tools_row.addWidget(self.save_button)
        chart_layout.addWidget(head)
        body = QVBoxLayout()
        body.setContentsMargins(10, 8, 12, 6)
        body.setSpacing(6)
        chart_layout.addLayout(body, 1)
        self.plot = OscilloscopePlot()
        self.plot.plot.snapshot_provider = self.export_snapshot
        self.plot.tag_pressed.connect(self.begin_tag_drag)
        self.plot.tag_cancelled.connect(self.cancel_tag_drag)
        self.plot.ground_dragged.connect(self.drag_offset)
        self.plot.trigger_dragged.connect(self.drag_trigger)
        self.plot.channel_selected.connect(self._select_channel_card)
        body.addWidget(self.plot, 1)
        foot = QFrame(chart)
        foot.setObjectName("cardFoot")
        footer = QHBoxLayout(foot)
        footer.setContentsMargins(14, 8, 16, 8)
        footer.setSpacing(6)
        self.status_dot = Dot("idle", 7)
        footer.addWidget(self.status_dot)
        self.status = QLabel("监测未开始")
        self.status.setObjectName("chartStatus")
        self.status.setWordWrap(True)
        footer.addWidget(self.status, 1)
        self.actual = QLabel("实际 — s/帧")
        self.actual.setObjectName("chartMeta")
        footer.addWidget(self.actual)
        chart_layout.addWidget(foot)
        self.splitter.addWidget(chart)
        self.right = QWidget()
        self.right.setMinimumWidth(340)
        self.right.setMaximumWidth(380)
        self.right_layout = QVBoxLayout(self.right)
        self.right_layout.setContentsMargins(0, 0, 0, 0)
        self.splitter.addWidget(self.right)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 0)
        self.splitter.setSizes([830, 352])
        layout.addWidget(self.splitter, 1)
        self.selector.selected.connect(self.select_device)
        controller.refreshed.connect(self.refresh)
        controller.frame_ready.connect(self.refresh_frame)
        self.select_device(self.selector.device_id)

    def select_device(self, device_id: str) -> None:
        if (
            self.single_job
            or self.pausing_for_single
            or self.memory_busy
            or self.controls
            and self.controls.running
        ):
            return
        if (self.device_id, self.mode) == (device_id, self.controller.mode) and self.controls:
            return
        self.cancel_tag_drag()
        if isinstance(self.controls, PicoScopeControls):
            self.pico_drafts[self.mode, self.device_id] = self.controls.draft()
        self.single_notice = ""
        self.frozen_at = 0.0
        self.device_id, self.mode = device_id, self.controller.mode
        self.timing.bind(device_id, self.mode)
        self.plot.hidden_channels.clear()
        self.plot.reset()
        if self.controls:
            self.controls.hide()
            self.right_layout.removeWidget(self.controls)
            self.controls.deleteLater()
            self.controls = None
        self.source = None
        self.splitter.setVisible(bool(device_id))
        self.empty.setVisible(not device_id)
        self.frozen_at = 0.0
        self.single_notice = ""
        if device_id:
            config = config_from_record(self.controller.records[device_id])
            self.source = next((s for s in data_sources(config) if s.kind == "waveform"), None)
            if config.adapter == "pico.6404e":
                self.controls = PicoScopeControls(
                    self.controller, device_id, self.pico_drafts.get((self.mode, device_id)), self.right
                )
                self.controls.source_changed.connect(self.select_pico_source)
                self.select_pico_source()
            else:
                self.controls = OscilloscopeControls(self.controller, device_id, self.start, self.right)
                self.controls.preview_requested.connect(self.plot.preview_parameter)
                self.controls.preview_cancelled.connect(self.plot.clear_preview)
                self.controls.control_finished.connect(self.preview_finished)
            self.right_layout.addWidget(self.controls)
            self.controls.busy_changed.connect(self.control_busy_changed)
            self.controls.channels_changed.connect(self.channels_changed)
        if self.isVisible():
            self.focus_settings()
        self.refresh()
        self.refresh_frame(device_id)

    def frame_is_current(self, state: dict) -> bool:
        frame = self.plot.displayed_frame
        return bool(
            frame
            and frame.session_id == state.get("sessionId")
            and frame.configuration_version == state.get("configurationVersion")
        )

    def tag_context(self, channel: int, operation: str, state: dict) -> TagDrag | None:
        controls = self.controls
        if not isinstance(controls, OscilloscopeControls) or not self.frame_is_current(state):
            return None
        if (
            controls.running
            or self.memory_busy
            or self.single_job
            or self.pausing_for_single
            or state.get("connectionState") not in {"connected", "busy"}
            or state.get("controlsLocked")
            or state.get("controlGroup")
            or state.get("controlPending")
            or state.get("capabilityStale")
        ):
            return None
        session, version = state["sessionId"], state["configurationVersion"]
        if controls.last_context != (session, version):
            return None
        display = self.plot.display_specs.get(channel)
        if operation == "trigger_level":
            if controls.trigger_channel() != channel:
                return None
            field = controls.trigger_slider
        else:
            if display is None or display.vertical_offset_sign is None:
                return None
            field = controls.sliders.get((channel, "offset"))
        if display is None or field is None or not field.isEnabled() or field.actual is None:
            return None
        return TagDrag(self.device_id, session, version, channel, operation, display, field)

    def latest_tag_context(self, channel: int, operation: str) -> TagDrag | None:
        # A worker can advance the configuration before the next controller poll.
        states = self.controller.runtime.sessions.snapshots()
        state: dict = next((s for s in states if s["stableDeviceId"] == self.device_id), {})
        return self.tag_context(channel, operation, state)

    def begin_tag_drag(self, channel: int, operation: str) -> None:
        self.cancel_tag_drag()
        drag = self.latest_tag_context(channel, operation)
        if drag is not None:
            drag.field.timer.stop()
            if drag.field.dirty:
                drag.field.restore_actual()  # Discard any pending keyboard edit on this field.
            drag.field.tag_active = True
            self.tag_drag = drag

    def cancel_tag_drag(self) -> None:
        drag, self.tag_drag = self.tag_drag, None
        if drag is not None:
            tag = (
                self.plot.trigger_tag
                if drag.operation == "trigger_level"
                else self.plot.ground_tags.get(drag.channel)
            )
            if tag is not None:
                tag.cancel_drag()
            drag.field.restore_actual()

    def move_tag(self, channel: int | None, operation: str, value: float, finished: bool) -> None:
        drag = self.tag_drag
        if drag is None:
            return
        if (
            channel != drag.channel
            or operation != drag.operation
            or not drag.field.tag_active
            or (finished and self.latest_tag_context(channel, operation) != drag)
        ):
            self.cancel_tag_drag()
            return
        drag.field.set_target(value)
        if finished:
            self.tag_drag = None
            drag.field.tag_active = False
            drag.field.commit()

    def drag_offset(self, channel: int, value: float, finished: bool) -> None:
        self.move_tag(channel, "offset", value, finished)

    def drag_trigger(self, value: float, finished: bool) -> None:
        self.move_tag(self.plot.trigger_state[0], "trigger_level", value, finished)

    def control_busy_changed(self, busy):
        self.selector.setEnabled(not (busy or self.single_job or self.pausing_for_single))
        self.refresh()

    def select_pico_source(self):
        if isinstance(self.controls, PicoScopeControls):
            kind = self.controls.capture_mode.currentData()
            config = config_from_record(self.controller.records[self.device_id])
            self.source = next(s for s in data_sources(config) if s.kind == kind)

    def focus_settings(self) -> None:
        self.controller.focus_settings(self.device_id, self.controls.keys() if self.controls else ())

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self.focus_settings()
        self.refresh_frame(self.device_id)

    def hideEvent(self, event) -> None:  # noqa: N802
        super().hideEvent(event)
        self.cancel_tag_drag()
        self.plot.clear_preview()
        if self.controller.sync_device == self.device_id:
            self.controller.focus_settings("")

    def focus_card(self, spec: dict) -> None:
        channel = next(iter(spec.get("channels", ())), 1)
        self.selector.select(spec["device_id"], channel)

        if self.controls and spec.get("channels"):
            check = self.controls.checks.get(spec["channels"][0])
            if check:
                self.controls.show_tab("垂直")
                self.controls.show_channel(channel)
                self.controls.scroller.ensureWidgetVisible(check)
                check.setFocus()

    def preview_finished(self, success: bool) -> None:
        if success:
            version = self.controller.states.get(self.device_id, {}).get("configurationVersion", 0)
            self.plot.commit_preview(version)
        else:
            self.plot.clear_preview()

    def channels_changed(self) -> None:
        if isinstance(self.controls, PicoScopeControls):
            # Pico channel switches edit the next capture, including after Single.
            self.refresh()
            return
        # A committed SCPI OFF must disappear immediately, including the all-off pause.
        if self.controls:
            channels = self.controls.channels()
            self.plot.restrict_channels(channels)
            self.plot.last_token = None
            self.plot.meta.setText("通道已更新 · 等待新波形" if channels else "未选择采集通道")
            self.refresh()

    def start(self, *, once=False, manual=False) -> None:
        if not self.source or not self.controls or not self.controls.channels():
            return
        try:
            self.controller.ensure_applicable(self.device_id)
            self.timing.interpretText()
            if isinstance(self.controls, PicoScopeControls):
                request = self.source.request(
                    self.controls.channels(), self.controls.points.value(), initiate=True, confirmed=True
                )
                device_id, session = self.device_id, self.controller.states[self.device_id]["sessionId"]

                def ready():
                    state = self.controller.states.get(device_id, {})
                    if self.device_id == device_id and state.get("sessionId") == session:
                        self.start_request(request, once, manual=manual)

                self.controls.prepare(ready)
            else:
                self.start_request(self.source.request(self.controls.channels()), once, manual=manual)
        except Exception as exc:
            self.controller.message.emit(str(exc))

    def request_single(self):
        if self.single_job or self.pausing_for_single or not self.single_action.isEnabled():
            return
        service = self.controller.runtime.monitoring
        task = service.snapshots().get(self.device_id, {})
        device = self.device_id
        if recording_phase(task) != "idle" or task.get("stopping"):
            return
        if task.get("running"):
            token = object()
            self.pausing_for_single = token
            self.refresh()

            def paused(_result=None, error=None):
                if self.pausing_for_single is not token:
                    return
                self.pausing_for_single = None
                if error:
                    self.single_notice = str(error)
                elif self.device_id == device:
                    self.start(once=True)
                self.refresh()

            self.controller.watch(service.stop(device), paused, lambda exc: paused(error=exc))
        else:
            self.start(once=True)

    def cancel_single(self):
        if self.pausing_for_single:
            self.pausing_for_single = None
            self.single_notice = "已取消本次采集"
            self.refresh()
        if self.single_job:
            self.single_job.cancel.set()

    def start_request(self, request, once, *, manual=False):
        if once:
            if self.single_job:
                return
            device = self.device_id
            session = self.controller.states[device]["sessionId"]
            job = SingleCapture(replace(request, confirmed=True), restore=False)
            future = self.controller.runtime.sessions.submit_single(device, job)
            self.single_job = job
            self.selector.setEnabled(False)
            self.refresh()

            def finished(batch=None, error=None):
                self.single_job = None
                self.selector.setEnabled(True)
                state = self.controller.states.get(device, {})
                if self.device_id == device and state.get("sessionId") == session:
                    self.single_notice = str(error) if error else "单次采集完成"
                    if batch is not None:
                        frame = MonitorFrame(
                            1,
                            batch.configuration.get("_session_configuration_version", 0),
                            request,
                            immutable_batch(batch),
                            session_id=session,
                        )
                        self.frozen_at = frame.received_at
                        self.plot.show_frame(frame, self.source, request.channels)
                        if isinstance(self.controls, PicoScopeControls):
                            self.controls.show_sample_rate(frame)
                self.refresh()

            self.controller.watch(future, finished, lambda exc: finished(error=exc))
            return
        self.single_notice = ""
        self.controller.start_monitoring(
            self.device_id,
            (request,),
            **self.timing.options(),
            mode=self.mode,
            once=once,
            ensure_continuous=manual,
        )
        self.refresh()

    def toggle_monitoring(self) -> None:
        if self.single_job or self.pausing_for_single:
            return
        if self.controller.reconnect_if_needed(self.device_id):
            return
        service = self.controller.runtime.monitoring
        if self.controls and self.controls.running:
            self.controls.restart = False
            self.controller.watch(service.stop(self.device_id))
            self.refresh()
            return
        if service.snapshots().get(self.device_id, {}).get("running"):
            self.controller.watch(service.stop(self.device_id))
        else:
            self.start(manual=True)

    def record(self) -> None:
        service = self.controller.runtime.monitoring
        try:
            self.controller.set_recording(
                self.device_id,
                not service.snapshots().get(self.device_id, {}).get("recording"),
                mode=self.mode,
            )
            self.refresh()
        except Exception as exc:
            self.controller.message.emit(str(exc))

    def export_snapshot(self):
        frame = self.plot.displayed_frame
        device, mode = self.device_id, self.mode
        if frame is None:
            return PlotExport([], "时间 (s)", "电压 (V)", save_raw=lambda: None)
        batch = mark_coverage(frame.batch)
        if any(m.metadata["coverage"]["partial"] for m in batch.measurements):
            frame = replace(frame, batch=batch)
        curves, partial = [], []
        for m in batch.measurements:
            if m.channel in self.plot.hidden_channels:
                continue
            info = m.metadata["coverage"]
            curves.append((f"CH{m.channel}", m.axis, m.values))
            if info["partial"]:
                partial.append(f"CH{m.channel} {info['first_s']:.6g} 至 {info['last_s']:.6g} s")
        unit = batch.measurements[0].unit
        axis = "时间 (s)" + (" · 部分波形 " + " / ".join(partial) if partial else "")
        return PlotExport(
            curves, axis, f"幅值 ({unit})", save_raw=lambda: self.save_frame(device, mode, frame)
        )

    def save(self):
        if self.plot.displayed_frame is not None:
            self.export_snapshot().save_raw()

    def save_frame(self, device, mode, frame):
        self.controller.save_displayed_frame(self, device, frame, mode)

    def read_memory(self) -> None:
        if not self.controls or self.memory_busy:
            return
        device_id = self.device_id
        try:
            request = AcquisitionRequest(
                "waveform", self.controls.channels(), confirmed=True, waveform_extent="memory"
            )
            future = self.controller.runtime.monitoring.read_memory_waveform(device_id, request)
            self.memory_busy = True
            self.selector.setEnabled(False)

            def finished(frame=None):
                self.memory_busy = False
                self.selector.setEnabled(True)
                if frame is not None and self.source and self.controls and device_id == self.device_id:
                    self.plot.show_frame(frame, self.source, self.controls.channels())
                self.refresh()

            self.controller.watch(future, finished, lambda _exc: finished())
            self.refresh()
        except Exception as exc:
            self.controller.message.emit(str(exc))

    def refresh(self) -> None:
        if (self.device_id, self.mode) != (self.selector.device_id, self.controller.mode) and not (
            self.memory_busy or self.controls and self.controls.running
        ):
            self.select_device(self.selector.device_id)
            return
        state = self.controller.states.get(self.device_id, {})
        if state.get("controlsLocked") or state.get("connectionState") not in {"connected", "busy"}:
            self.plot.clear_preview()
        task = self.controller.runtime.monitoring.snapshots().get(self.device_id, {})
        busy = self.memory_busy or bool(self.controls and self.controls.running)
        snapshot = self.controller.runtime.sessions.capability_snapshot(self.device_id)
        memory_supported = bool(snapshot and snapshot.details.get("memory_waveform"))
        connected = state.get("connectionState") in {"connected", "busy"}
        if isinstance(self.controls, PicoScopeControls):
            self.controls.update_available(task, connected, bool(state.get("controlsLocked")))
        self.single_action.setEnabled(
            bool(
                connected
                and snapshot
                and snapshot.details.get("single_acquisition", True)
                and self.controls
                and self.controls.channels()
                and not busy
                and not self.single_job
                and not self.pausing_for_single
                and not state.get("controlsLocked")
                and not task.get("stopping")
                and recording_phase(task) == "idle"
            )
        )
        self.cancel_action.setVisible(self.single_job is not None or self.pausing_for_single is not None)
        self.memory_action.setVisible(memory_supported)
        self.memory_action.setEnabled(
            connected
            and not busy
            and not self.single_job
            and not self.pausing_for_single
            and not task.get("stopping")
            and recording_phase(task) == "idle"
            and not state.get("controlsLocked")
            and bool(self.controls and self.controls.channels())
        )
        self.connection.setText(
            "已连接" if connected else state_label(state.get("connectionState", "disconnected"))
        )
        self.start_button.setText("暂停监测" if task.get("running") else "开始监测")
        self.start_button.setEnabled(
            bool(
                (connected or task.get("running"))
                and not busy
                and (task.get("running") or self.controls and self.controls.channels())
                and not (task.get("pending") and not task.get("running"))
            )
        )
        if self.controls and self.controls.running and self.controls.restart:
            self.start_button.setText("取消续监测")
            self.start_button.setEnabled(bool(connected))
        refresh_reconnect_button(
            self.start_button,
            self.controller,
            self.device_id,
            task,
            busy=busy,
            can_cancel=bool(self.controls and self.controls.running and self.controls.restart),
        )
        if self.single_job or self.pausing_for_single:
            self.start_button.setEnabled(True)
            self.start_button.setText(self.single_job.phase if self.single_job else "准备单次采集")
        run_icon = "pause" if task.get("running") else "play"
        if self.start_button.property("kitIcon") != run_icon:
            self.start_button.setProperty("kitIcon", run_icon)
            self.start_button.setIcon(icons.icon(run_icon, tk.TEXT))
        self.record_button.refresh(
            task, connected=connected, busy=busy, locked=bool(state.get("controlsLocked"))
        )
        self.refresh_tag_state(state)
        update_recording_frame(self.chart, task)
        self.save_button.setEnabled(
            self.plot.displayed_frame is not None
            and not task.get("display_saving")
            and not task.get("capture_saving")
        )
        self.fit_button.setEnabled(self.plot.displayed_frame is not None)
        self.plot.save_enabled = self.save_button.isEnabled()
        self.update_readouts()
        self.timing.refresh(task)
        actual_text = actual_interval_text(task)
        clock = self.plot.last_frame_clock
        self.actual.setText(f"最新帧 {clock} · {actual_text}" if clock else actual_text)
        self._sync_channel_cards()
        old = self.plot.displayed_frame
        frozen = old is not None and old.batch.performance.get("instrument_running") is False
        waiting = old is not None and not self.frame_is_current(state)
        self.status.setText(
            ("未连接" if not connected else "")
            or ("准备单次采集" if self.pausing_for_single else "")
            or (self.single_job.phase if self.single_job else "")
            or task.get("error")
            or task.get("notice")
            or task.get("recording_notice")
            or recording_save_notice(task)
            or (self.single_notice if not task.get("running") and not waiting else "")
            or (
                "完整波形读取中 · 完成后自动续采"
                if self.memory_busy
                else "设置中 · 暂停后续读取"
                if busy
                else "等待当前配置的新帧 · 保留旧波形及刻度"
                if waiting
                else "记录收尾中 · 正在排空已接收数据"
                if recording_phase(task) == "draining"
                else "记录中 · 仪器已停止，保存的是冻结波形"
                if task.get("recording") and frozen
                else "记录中 · 逐帧保存"
                if task.get("recording")
                else "监测中 · 仪器已停止，当前为冻结波形；可在触发设置中选择 AUTO"
                if task.get("running") and frozen
                else "监测中"
                if task.get("running")
                else "监测已暂停"
            )
        )
        self.status_dot.set_status(
            "err"
            if task.get("error") or not connected or task.get("recording")
            else "warn"
            if busy or waiting or self.single_job
            else "ok"
            if task.get("running")
            else "idle",
            live=bool(task.get("running")),
        )

    def update_readouts(self) -> None:
        """Timebase, sample rate, points and trigger from the verified readback values."""
        controls = self.controls
        frame = self.plot.displayed_frame
        state = self.controller.states.get(self.device_id, {})
        if frame and (
            frame.session_id != state.get("sessionId")
            or (isinstance(controls, PicoScopeControls) and not self.frame_is_current(state))
        ):
            frame = None
        counts = {len(m.values) for m in frame.batch.measurements} if frame else set()
        actual_pts = " / ".join(f"{n:,}" for n in sorted(counts)) if counts else ""
        extent = ""
        if frame and frame.batch.measurements:
            extent = {"screen": "屏幕", "memory": "存储"}.get(
                frame.batch.measurements[0].metadata.get("waveform_extent", ""), ""
            )
        if not isinstance(controls, OscilloscopeControls):
            config = frame.batch.configuration if frame else {}
            interval = config.get("sample_interval_s")
            rate_text = interval_text = "—"
            if isinstance(interval, (int, float)) and interval > 0:
                rate_text = engineering(1 / interval, "Sa/s")
                interval_text = engineering(interval, "s") + "/点"
            self.readouts.set("rate", rate_text)
            index = config.get("timebase")
            timebase_text = (
                f"索引 {index}"
                if index is not None and config.get("capture_mode") == "waveform"
                else interval_text
            )
            self.readouts.set("timebase", timebase_text)
            self.readouts.set("points", f"{frame.request.points:,}" if frame else "—")
            self.readouts.set(
                "actual_points",
                f"{actual_pts} · {extent}" if actual_pts and extent else (actual_pts or "—"),
            )
            self.readouts.set("trigger", "—")
            return
        values = controls.values
        timebase = values.get((1, "timebase"))
        self.readouts.set(
            "timebase",
            engineering(float(timebase), "s") + "/格" if isinstance(timebase, (int, float)) else "—",
        )
        rate = controls.sample_rate.text().partition("：")[2].strip()
        self.readouts.set("rate", rate or "—")
        points = values.get((1, "record_length"))
        if isinstance(points, str) and points.strip().isdigit():
            points = int(points.strip())
        target_pts = (
            f"{points:,.0f}"
            if isinstance(points, (int, float))
            else "AUTO"
            if str(points).strip().upper() == "AUTO"
            else ""
        )
        self.readouts.set("points", target_pts or "—")
        self.readouts.set(
            "actual_points",
            f"{actual_pts} · {extent}" if actual_pts and extent else (actual_pts or "—"),
        )
        channel, level, _enabled = controls.trigger_state()
        source = values.get((1, "trigger_source"))
        slope = values.get((1, "trigger_slope"))
        mode = values.get((1, "trigger_mode"))
        arrow = {"POS": "↑", "POSITIVE": "↑", "RISE": "↑", "NEG": "↓", "NEGATIVE": "↓", "FALL": "↓"}.get(
            str(slope).upper(), ""
        )
        head_part = " ".join(
            p
            for p in (
                f"CH{channel}" if channel else (str(source) if source else ""),
                (arrow + " " if arrow else "")
                + (engineering(float(level), "V") if level is not None else ""),
            )
            if p
        )
        mode_part = str(mode).title() if mode else ""
        self.readouts.set("trigger", head_part or mode_part or "—")
        if "trigger" in self.readouts.cells:
            full_tip = " · ".join(p for p in (head_part, mode_part) if p)
            self.readouts.cells["trigger"].setToolTip(full_tip)

    def refresh_tag_state(self, state: dict) -> None:
        drag = self.tag_drag
        if drag and self.tag_context(drag.channel, drag.operation, state) != drag:
            self.cancel_tag_drag()
        controls = self.controls
        if not isinstance(controls, OscilloscopeControls):
            self.plot.set_drag_state(frozenset(), (None, None, False))
            return
        channel, level, enabled = controls.trigger_state()
        offsets = frozenset(ch for ch in controls.offset_channels() if self.tag_context(ch, "offset", state))
        trigger_enabled = bool(enabled and channel and self.tag_context(channel, "trigger_level", state))
        self.plot.set_drag_state(offsets, (channel, level, trigger_enabled))

    def _select_channel_card(self, channel: int) -> None:
        if isinstance(self.controls, OscilloscopeControls):
            self.controls.show_tab("垂直")
            self.controls.show_channel(channel)
            self._sync_channel_cards()

    def _sync_channel_cards(self) -> None:
        controls = self.controls
        if controls is None:
            self.plot.sync_channel_cards((), set(), {})
            return
        all_channels = tuple(controls.instrument_channels)
        enabled = set(controls.channels())
        scales: dict[int, str] = {}
        selected_ch: int | None = None
        if isinstance(controls, OscilloscopeControls):
            for ch in all_channels:
                val = controls.values.get((ch, "scale"))
                if isinstance(val, (int, float)):
                    scales[ch] = f"{engineering(float(val), 'V')}/格"
            selected_ch = controls.focused_channel
        self.plot.sync_channel_cards(
            all_channels,
            enabled,
            scales,
            selected_channel=selected_ch,
        )

    def refresh_frame(self, device_id: str) -> None:
        if device_id != self.device_id or not self.isVisible() or not self.source or not self.controls:
            return
        service = self.controller.runtime.monitoring
        frame = service.latest(device_id, self.source.kind, self.source.quantity)
        if frame and not self.single_job and frame.received_at > self.frozen_at:
            pico = isinstance(self.controls, PicoScopeControls)
            channels = frame.request.channels if pico else self.controls.channels()
            self.plot.show_frame(frame, self.source, channels)
            state = self.controller.states.get(device_id, {})
            self.refresh_tag_state(state)
            self._sync_channel_cards()
            clock = self.plot.last_frame_clock
            if clock:
                task = service.snapshots().get(device_id, {})
                self.fit_button.setEnabled(True)
                self.plot.save_enabled = not (task.get("display_saving") or task.get("capture_saving"))
                self.save_button.setEnabled(self.plot.save_enabled)
                self.actual.setText(f"最新帧 {clock} · {actual_interval_text(task)}")
            if isinstance(self.controls, PicoScopeControls):
                self.controls.show_sample_rate(frame)
            self.update_readouts()
            service.displayed(device_id, frame)
