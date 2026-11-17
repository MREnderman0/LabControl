"""Oscilloscope workspace: passive monitoring and explicit instrument gestures."""

from dataclasses import replace

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
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
from lab_control.domain.waveform_coverage import mark_coverage
from lab_control.instruments.scope_single import SingleCapture
from lab_control.ui.controller import state_label
from lab_control.ui.pages.monitoring.connection import refresh_reconnect_button
from lab_control.ui.pages.monitoring.oscilloscope_controls import OscilloscopeControls
from lab_control.ui.pages.monitoring.oscilloscope_plot import OscilloscopePlot
from lab_control.ui.pages.monitoring.pico_scope_controls import PicoScopeControls
from lab_control.ui.pages.monitoring.recording_state import (
    RecordingButton,
    recording_phase,
    recording_save_notice,
    update_recording_frame,
)
from lab_control.ui.pages.monitoring.timing import MonitoringTiming
from lab_control.ui.widgets.device_selector import DeviceSelector
from lab_control.ui.widgets.plot_export import PlotExport


class OscilloscopeMonitoringPage(QWidget):
    def __init__(self, controller, add_card, parent=None) -> None:
        super().__init__(parent)
        self.controller, self.add_card = controller, add_card
        self.device_id, self.mode = "", controller.mode
        self.pico_drafts: dict[tuple[str, str], dict] = {}
        self.controls: OscilloscopeControls | PicoScopeControls | None = None
        self.source: DataSource | None = None
        self.memory_busy = False
        self.plot: OscilloscopePlot
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 12)
        layout.setSpacing(8)
        header = QHBoxLayout()
        self.selector = DeviceSelector(controller, "oscilloscope", self, compact=True)
        header.addWidget(self.selector, 1)
        self.connection = QLabel()
        self.connection.setObjectName("sourceConnectionState")
        header.addWidget(self.connection)
        self.single_job = None
        self.single_notice = ""
        self.pausing_for_single = None
        self.frozen_at = 0.0
        self.start_button = QToolButton()
        self.start_button.setText("开始监测")
        self.start_button.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        actions = QMenu(self.start_button)
        self.single_action = actions.addAction("单次采集", self.request_single)
        self.single_action.setToolTip("Single：仪器采集一帧后停止，软件读取这一帧")
        self.cancel_action = actions.addAction("取消本次采集", self.cancel_single)
        self.start_button.setMenu(actions)
        self.start_button.clicked.connect(self.toggle_monitoring)
        self.single_button = QToolButton()
        self.single_button.setObjectName("singleButton")
        self.single_button.setDefaultAction(self.single_action)
        self.single_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        header.addWidget(self.single_button)
        header.addWidget(self.start_button)
        layout.addLayout(header)
        self.empty = QLabel("请先从总览连接示波器，再选择设备。")
        layout.addWidget(self.empty)
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        chart = self.chart = QFrame()
        chart.setObjectName("spectrumChart")
        chart.setMinimumWidth(280)
        chart_layout = QVBoxLayout(chart)
        chart_layout.setContentsMargins(10, 8, 10, 8)
        tools_row = QHBoxLayout()
        title = QLabel("时域波形")
        title.setObjectName("spectrumChartTitle")
        tools_row.addWidget(title)
        tools_row.addStretch()
        self.record_button = RecordingButton(chart)
        self.record_button.clicked.connect(self.record)
        tools_row.addWidget(self.record_button)
        tools = QToolButton()
        tools.setObjectName("curveTools")
        tools.setProperty("buttonSize", "compact")
        tools.setText("曲线工具")
        tools.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(tools)
        self.save_action = menu.addAction("保存当前帧", self.save)
        self.memory_action = menu.addAction("读取完整存储波形", self.read_memory)
        self.memory_action.setVisible(False)
        menu.addSeparator()
        self.refresh_action = menu.addAction("刷新参数", self.refresh_parameters)
        self.fit_action = menu.addAction("恢复仪器视图", lambda: self.plot.clear_preview())
        self.timing_action = menu.addAction("采集节奏")
        self.timing_action.setCheckable(True)
        tools.setMenu(menu)
        tools_row.addWidget(tools)
        chart_layout.addLayout(tools_row)
        self.timing = MonitoringTiming(controller)
        self.timing.hide()
        self.timing_action.toggled.connect(self.timing.setVisible)
        chart_layout.addWidget(self.timing)
        self.plot = OscilloscopePlot()
        self.plot.plot.snapshot_provider = self.export_snapshot
        self.plot.ground_dragged.connect(self.drag_offset)
        self.plot.trigger_dragged.connect(self.drag_trigger)
        chart_layout.addWidget(self.plot, 1)
        footer = QHBoxLayout()
        self.status = QLabel("监测未开始")
        self.status.setObjectName("mutedText")
        self.status.setWordWrap(True)
        footer.addWidget(self.status, 1)
        self.actual = QLabel("实际 — s/帧")
        self.actual.setObjectName("rangeHint")
        footer.addWidget(self.actual)
        chart_layout.addLayout(footer)
        self.splitter.addWidget(chart)
        self.right = QWidget()
        self.right.setMinimumWidth(300)
        self.right.setMaximumWidth(360)
        self.right_layout = QVBoxLayout(self.right)
        self.right_layout.setContentsMargins(6, 0, 0, 0)
        self.splitter.addWidget(self.right)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setSizes([850, 330])
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
        if isinstance(self.controls, PicoScopeControls):
            self.pico_drafts[self.mode, self.device_id] = self.controls.draft()
        self.single_notice = ""
        self.frozen_at = 0.0
        self.device_id, self.mode = device_id, self.controller.mode
        self.timing.bind(device_id, self.mode)
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
        self.refresh_action.setVisible(True)
        if device_id:
            config = config_from_record(self.controller.records[device_id])
            self.source = next((s for s in data_sources(config) if s.kind == "waveform"), None)
            if config.adapter == "pico.6404e":
                self.controls = PicoScopeControls(
                    self.controller, device_id, self.pico_drafts.get((self.mode, device_id)), self.right
                )
                self.controls.source_changed.connect(self.select_pico_source)
                self.select_pico_source()
                self.refresh_action.setVisible(False)
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

    def drag_offset(self, channel: int, value: float, finished: bool) -> None:
        if isinstance(self.controls, OscilloscopeControls):
            self.controls.drag_field(self.controls.sliders.get((channel, "offset")), value, finished)

    def drag_trigger(self, value: float, finished: bool) -> None:
        if isinstance(self.controls, OscilloscopeControls):
            self.controls.drag_field(self.controls.trigger_slider, value, finished)

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

    def refresh_parameters(self) -> None:
        if isinstance(self.controls, OscilloscopeControls):
            self.controls.refresh_parameters()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self.focus_settings()
        self.refresh_frame(self.device_id)

    def hideEvent(self, event) -> None:  # noqa: N802
        super().hideEvent(event)
        self.plot.clear_preview()
        if self.controller.sync_device == self.device_id:
            self.controller.focus_settings("")

    def focus_card(self, spec: dict) -> None:
        self.selector.select(spec["device_id"])

        if self.controls and spec.get("channels"):
            check = self.controls.checks.get(spec["channels"][0])
            if check:
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
            self.timing.commit()
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
        try:
            self.controller.watch(
                self.controller.runtime.monitoring.export_displayed_frame(device, frame, mode),
                lambda path: self.controller.message.emit(f"原始数据已保存 · 分析 CSV 已完成：{path}"),
            )
        except Exception as exc:
            self.controller.message.emit(str(exc))

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

    def pin(self) -> None:
        if self.source and self.controls and self.controls.channels():
            self.add_card(self.device_id, self.source.key, self.controls.channels())

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
        if self.single_job:
            self.start_button.setEnabled(True)
            self.start_button.setText(self.single_job.phase)
        self.record_button.refresh(
            task, connected=connected, busy=busy, locked=bool(state.get("controlsLocked"))
        )
        if isinstance(self.controls, OscilloscopeControls) and connected and not busy:
            self.plot.set_drag_state(self.controls.offset_channels(), self.controls.trigger_state())
        else:
            channel, level, _enabled = (
                self.controls.trigger_state()
                if isinstance(self.controls, OscilloscopeControls)
                else (None, None, False)
            )
            self.plot.set_drag_state(frozenset(), (channel, level, False))
        update_recording_frame(self.chart, task)
        self.save_action.setEnabled(
            self.plot.displayed_frame is not None
            and not task.get("display_saving")
            and not task.get("capture_saving")
        )
        self.refresh_action.setEnabled(bool(connected and not busy and not state.get("controlsLocked")))
        self.timing.refresh(task)
        self.actual.setText(self.timing.actual.text())
        old = self.plot.displayed_frame
        frozen = old is not None and old.batch.performance.get("instrument_running") is False
        waiting = old is not None and (
            old.session_id != state.get("sessionId")
            or old.configuration_version != state.get("configurationVersion")
        )
        self.status.setText(
            (self.single_job.phase if self.single_job else self.single_notice)
            or task.get("error")
            or task.get("notice")
            or task.get("recording_notice")
            or recording_save_notice(task)
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

    def refresh_frame(self, device_id: str) -> None:
        if device_id != self.device_id or not self.isVisible() or not self.source or not self.controls:
            return
        service = self.controller.runtime.monitoring
        frame = service.latest(device_id, self.source.kind, self.source.quantity)
        if frame and not self.single_job and frame.received_at > self.frozen_at:
            pico = isinstance(self.controls, PicoScopeControls)
            channels = frame.request.channels if pico else self.controls.channels()
            self.plot.show_frame(frame, self.source, channels)
            if isinstance(self.controls, PicoScopeControls):
                self.controls.show_sample_rate(frame)
            service.displayed(device_id, frame)
