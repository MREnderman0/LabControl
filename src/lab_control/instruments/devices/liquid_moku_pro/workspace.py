"""Three views over a single connected inventory id. Opening a page performs no I/O."""

from math import isfinite

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMessageBox,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from lab_control.application.controls import data_sources
from lab_control.application.monitoring_state import MonitorFrame
from lab_control.domain.connections import ConnectionConfig
from lab_control.domain.instruments import AcquisitionRequest, DeviceCommand
from lab_control.domain.oscilloscope import engineering
from lab_control.domain.spectrum import SpectrumMarker
from lab_control.ui.pages.monitoring.spectrum import SpectrumMarkerActions

from .adapter import LABELS
from .capture_controls import CaptureControls
from .capture_settings import field
from .display_controls import MokuScopePlot, MokuSpectrumPlot, waveform_measurements
from .protocol import CHANNELS


class RolePage(SpectrumMarkerActions, QWidget):
    marker_list: QListWidget

    def __init__(self, controller, device_id, role):
        super().__init__()
        self.controller, self.device_id, self.role = controller, device_id, role
        self.service = controller.runtime.monitoring
        self.kind = {"oscilloscope": "waveform", "spectrum_analyzer": "spectrum"}.get(role, "")
        self._start_ticket = None
        self.trigger_receipt = None
        self.last: MonitorFrame | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 12)
        layout.setSpacing(8)
        title = QLabel(f"Moku:Pro · {LABELS[role]}")
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        row = QHBoxLayout()
        layout.addLayout(row)
        row.addWidget(QLabel("通道"))
        self.channels = QComboBox()
        for ch in CHANNELS:
            self.channels.addItem(f"ch{ch}", ch)
        row.addWidget(self.channels)
        row.addStretch()
        if self.kind:
            self.monitor_button = self._button(row, "开始监测", lambda: self.start(False))
            self.stop_button = self._button(row, "停止本面板", self.stop)
            self.build_measurement(layout)
        else:
            row = QHBoxLayout()
            layout.addLayout(row)
            self.freq = QDoubleSpinBox()
            self.freq.setRange(1, 1e6)
            self.freq.setValue(10000)
            self.freq.setSuffix(" Hz")
            self.amp = QDoubleSpinBox()
            self.amp.setRange(4, 100)
            self.amp.setValue(100)
            self.amp.setSuffix(" mVpp")
            for w in (QLabel("正弦 · 零偏置"), self.freq, self.amp):
                row.addWidget(w)
            self._button(
                row,
                "开启输出",
                lambda: self.command(
                    "standard_waveform",
                    {"frequency": self.freq.value(), "amplitude": self.amp.value() / 1000},
                    f"自动分配信号源 ch{self.channel} 的输出路由。确认实际负载为 50 Ω，输出 {self.freq.value():g} Hz、{self.amp.value():g} mVpp、零偏置？",
                ),
            )
            self._button(
                row,
                "关闭所选通道",
                lambda: self.command("waveform_stop", {}, f"关闭信号源 ch{self.channel}？"),
            )
        row.addStretch()
        self.hint = QLabel()
        self.hint.setWordWrap(True)
        if self.kind:
            self.hint.hide()
        else:
            layout.addWidget(self.hint)
        self.status = QLabel()
        self.status.setWordWrap(True)
        if self.kind:
            self.chart_layout.addWidget(self.status)
        else:
            layout.addWidget(self.status)
        if not self.kind:
            layout.addStretch()
        controller.refreshed.connect(self.refresh_status)

    def build_measurement(self, layout):
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.chart = QFrame()
        self.chart.setObjectName("spectrumChart")
        self.chart.setMinimumWidth(280)
        self.chart_layout = QVBoxLayout(self.chart)
        self.chart_layout.setContentsMargins(10, 8, 10, 8)
        toolbar = QHBoxLayout()
        title = QLabel("时域波形" if self.kind == "waveform" else "频谱")
        title.setObjectName("spectrumChartTitle")
        toolbar.addWidget(title)
        toolbar.addStretch()
        self.record_button = self._button(toolbar, "逐帧记录", lambda: self.start(True))
        self.export_button = self._button(toolbar, "导出当前帧", self.export)
        self.chart_layout.addLayout(toolbar)
        self.summary = QLabel()
        self.summary.setObjectName("rangeHint")
        self.summary.setWordWrap(True)
        self.chart_layout.addWidget(self.summary)
        if self.kind == "spectrum":
            self.marker_summary = QLabel()
            self.marker_summary.setObjectName("spectrumChartTitle")
            self.marker_summary.hide()
            self.chart_layout.addWidget(self.marker_summary)
        config = ConnectionConfig(**self.controller.records[self.device_id]["config"])
        self.source = next(s for s in data_sources(config) if s.kind == self.kind)
        self.plot = MokuScopePlot() if self.kind == "waveform" else MokuSpectrumPlot()
        self.chart_layout.addWidget(self.plot, 1)
        self.splitter.addWidget(self.chart)
        self.capture = CaptureControls(self)
        self.settings = self.capture.settings
        self.readback = self.capture.readback
        if isinstance(self.plot, MokuSpectrumPlot):
            self.marker_plot = self.plot
            self.markers = []
            self.marker_list.itemDoubleClicked.connect(
                lambda item: self.edit_marker(item.data(Qt.ItemDataRole.UserRole))
            )
            self.marker_plot.add_frequency.connect(self.add_marker_at)
            self.marker_plot.edit_marker.connect(self.edit_marker)
            self.marker_plot.save_frame.connect(self.export)
            self.marker_plot.readings_changed.connect(self.update_marker_list)
        self.splitter.addWidget(self.capture)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setSizes([800, 395])
        layout.addWidget(self.splitter, 1)
        self.channels.currentIndexChanged.connect(self.clear_curve)
        self.clear_curve()
        self.controller.frame_ready.connect(self.refresh_plot)

    def show_trigger(self, actual, channel):
        self.trigger_receipt = (self.state.get("sessionId"), actual, channel)
        if self.last is not None and isinstance(self.plot, MokuScopePlot):
            self.plot.display_trigger(channel, actual["level"])

    def update_marker_list(self):
        super().update_marker_list()
        rows = min(3, self.marker_list.count())
        height = sum(self.marker_list.sizeHintForRow(i) + 2 * self.marker_list.spacing() for i in range(rows))
        self.marker_list.setFixedHeight(max(54, height + 12))
        readings = []
        for marker in self.markers:
            reading = self.marker_plot.readings.get(marker.number)
            if reading and reading.position is not None and reading.amplitude is not None:
                readings.append(
                    f"M{marker.number}　{engineering(reading.position, 'Hz')}　{reading.amplitude:.2f} {self.marker_plot.amplitude_unit}"
                )
        self.marker_summary.setText("　·　".join(readings[:2]))
        self.marker_summary.setVisible(bool(readings))

    def add_center_marker(self):
        trace = self.marker_plot.trace
        self.add_marker_at(float((trace.axis[0] + trace.axis[-1]) / 2) if trace else 0.0)

    def add_marker_at(self, frequency):
        trace = self.marker_plot.trace
        width = float(trace.axis[-1] - trace.axis[0]) if trace else 20.0
        marker = SpectrumMarker(
            max((m.number for m in self.markers), default=0) + 1, frequency, half_width=max(1.0, width * 0.05)
        )
        self.marker_dialog(marker, creating=True)

    @property
    def channel(self):
        return self.channels.currentData()

    def _button(self, row, title, action):
        button = QPushButton(title)

        def clicked():
            try:
                action()
            except Exception as exc:
                self.status.setText(str(exc))

        button.clicked.connect(clicked)
        row.addWidget(button)
        return button

    @property
    def state(self):
        return self.controller.states.get(self.device_id, {})

    def _attached(self):
        return self.state.get("capabilityDetails", {}).get("workspace_attached", False)

    def _confirm(self, message):
        if not self._attached():
            message = "首次使用将连接已有四槽，不部署、不强占。请确认原配置已备份且可恢复。\n" + message
        return (
            QMessageBox.question(
                self,
                LABELS[self.role],
                message,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            == QMessageBox.StandardButton.Yes
        )

    def command(self, op, params, message, *, channel=None, on_success=None):
        ch = self.channel if channel is None else channel
        if not self._confirm(message):
            return

        def finished(result):
            if result.outcome != "success":
                self.status.setText(result.error)
                return
            self.status.setText(f"CH {ch} · 设备已确认命令")
            if on_success:
                on_success(result)

        try:
            self.controller.apply(
                self.device_id,
                DeviceCommand(op, params, ch, confirmed=True),
                finished,
                lambda e: self.status.setText(str(e)),
            )
        except Exception as exc:
            self.status.setText(str(exc))

    def start(self, recording):
        if self._start_ticket is not None:
            return
        ch = self.channel
        if not self._attached() and not self._confirm(
            f"自动连接{LABELS[self.role]} ch{ch} 并开始读取，不会开启任何输出。"
        ):
            return
        ticket = self._start_ticket = object()
        session_id = self.state.get("sessionId")
        stop_revision = self.service.stop_revision(self.device_id)
        self.status.setText(f"正在核对{LABELS[self.role]} ch{ch} 的输入路由…")
        self.refresh_status()

        def failed(exc):
            if self._start_ticket is ticket:
                self._start_ticket = None
                self.refresh_status()
                self.status.setText(str(exc))

        def ready():
            if self._start_ticket is not ticket:
                return
            self._start_ticket = None
            self.refresh_status()
            if (
                self.state.get("sessionId") != session_id
                or self.service.stop_revision(self.device_id) != stop_revision
            ):
                self.status.setText("会话已更换或监测已停止，未启动采集")
                return
            try:
                future = self.service.start_role(
                    self.device_id,
                    AcquisitionRequest(self.kind, (ch,)),
                    recording=recording,
                    mode=self.controller.mode,
                )
                if future:
                    self.controller.watch(future, on_error=lambda e: self.status.setText(str(e)))
                self.refresh_status()
                self.status.setText("逐帧记录" if recording else "监测中")
            except Exception as exc:
                self.status.setText(str(exc))

        def verified(result):
            if ch not in result.values.get("measurement_inputs", {}).get(self.kind, ()):
                failed("记录期间不能更改输入路由。请先停止记录，再开始本通道的监测。")
            else:
                ready()

        try:
            if self.state.get("controlsLocked"):
                # A pre-routed second panel can join a recording without a control write.
                self.controller.watch(
                    self.controller.runtime.sessions.submit(self.device_id, "status"), verified, failed
                )
            else:
                self.controller.apply(
                    self.device_id,
                    DeviceCommand(
                        "select_measurement_input", {"role": self.kind.upper()}, ch, confirmed=True
                    ),
                    lambda result: ready() if result.outcome == "success" else failed(result.error),
                    failed,
                )
        except Exception as exc:
            failed(exc)

    def stop(self):
        self._start_ticket = None
        future = self.service.stop_role(self.device_id, self.kind)
        if future:
            self.controller.watch(future)
        self.status.setText("已请求停止本面板")
        self.refresh_status()

    def refresh_status(self):
        if not self.isVisible():
            return
        state = self.state
        connected = state.get("connectionState") in {"connected", "busy"}
        writable = connected and not (state.get("controlsLocked") or state.get("controlPending"))
        prefix = "已连接四槽。" if self._attached() else "首次使用时将核对并连接已有四槽。"
        self.hint.setText(
            prefix
            + (
                "开始监测或记录时自动连接所选输入，不开启任何输出。"
                if self.kind
                else "开启输出时自动分配所选端口；仅选择通道不会发信号。"
            )
        )
        if not self.kind:
            return
        snap = self.service.snapshots().get(self.device_id, {})
        pending = self._start_ticket is not None
        running = bool(snap.get("running") and self.kind in snap.get("kinds", ()))
        recording = self.kind in snap.get("recording_kinds", ())
        writable = (
            writable
            and not snap.get("recording_kinds")
            and self.device_id not in self.controller.group_devices
        )
        self.monitor_button.setEnabled(connected and not pending and not running)
        self.record_button.setEnabled(connected and not pending and not recording)
        self.record_button.setText("记录中" if recording else "逐帧记录")
        self.stop_button.setEnabled(pending or running)
        self.channels.setEnabled(not (pending or running))
        self.capture.bind(state.get("sessionId"), self.channel)
        self.capture.writable = writable and not pending
        self.capture.buttons()
        if self.capture.active is not None:
            self.channels.setEnabled(False)
        self.export_button.setEnabled(self.last is not None and not snap.get("display_saving"))
        if self.kind == "spectrum":
            self.marker_plot.save_enabled = self.export_button.isEnabled()
        if self.last is not None and self.service.latest(self.device_id, self.kind) is None:
            self.clear_curve()
        note = snap.get("error") or snap.get("recording_notice") or snap.get("notice")
        if note:
            self.status.setText(note)

    def clear_curve(self):
        self.plot.reset()
        self.summary.clear()
        self.readback.setText("未读回")
        self.settings.setText("等待首帧")
        self.last = None
        self.export_button.setEnabled(False)
        self.capture.bind(self.state.get("sessionId"), self.channel)
        if self.kind == "spectrum":
            self.marker_key = f"spectrum-markers/{self.controller.mode}/{self.device_id}/ch{self.channel}"
            self.markers = []
            for row in self.controller.runtime.repository.load_workspace(self.marker_key, []):
                try:
                    self.markers.append(SpectrumMarker(**row))
                except (ValueError, TypeError):
                    continue
            self.marker_plot.set_markers(self.markers)

    def show_settings(self, settings):
        def value(group, key, unit):
            raw = field(settings.get(group, {}), key)
            return engineering(raw, unit) if isinstance(raw, (int, float)) and isfinite(raw) else "—"

        if self.kind == "waveform":
            low, high = (value("timebase", k, "s") for k in ("t1", "t2"))
            mode = field(settings.get("acquisition_mode"), "mode") or "—"
            interpolation = field(settings.get("interpolation"), "interpolation") or "—"
            return f"时窗　{low} ～ {high}\n采样率　{value('samplerate', 'sample_rate', 'Sa/s')}\n采集　{mode} / {interpolation}"
        low, high = (value("span", k, "Hz") for k in ("frequency1", "frequency2"))
        window = settings.get("window", {}).get("window", "—")
        return f"频段　{low} ～ {high}\nRBW　{value('rbw', 'rbw_value', 'Hz')}\n窗函数　{window}"

    def refresh_plot(self, device_id):
        if device_id != self.device_id or not self.isVisible():
            return
        frame = self.service.latest(device_id, self.kind)
        if frame is None or frame.request.channels != (self.channel,):
            if self.last is not None:
                self.clear_curve()
            return
        if frame is self.last:
            return
        self.plot.show_frame(frame, self.source, (self.channel,))
        displayed = self.plot.displayed_frame
        if (
            displayed is None
            or displayed.batch.timestamp != frame.batch.timestamp
            or displayed.sequence != frame.sequence
        ):
            return
        f = frame.batch.configuration["frontend"]
        m = frame.batch.measurements[0]
        info = self.show_settings(frame.batch.configuration["settings"]).replace("\n", "　·　")
        if self.kind == "waveform":
            vpp, rms, frequency = waveform_measurements(m.values, m.axis)
            levels = f"CH{m.channel}　Vpp {engineering(vpp, 'V')}　RMS {engineering(rms, 'V')}"
            if frequency is not None:
                levels += "　" + engineering(frequency, "Hz")
            info += "\n" + levels
        if (
            isinstance(self.plot, MokuScopePlot)
            and self.trigger_receipt
            and self.trigger_receipt[0] == self.state.get("sessionId")
        ):
            _, actual, channel = self.trigger_receipt
            source = f"CH{channel}" if channel is not None else "外部"
            info += f"　·　触发 {source} {engineering(actual['level'], 'V')}"
            self.plot.display_trigger(channel, actual["level"])
        self.summary.setText(f"{info}　·　{m.values.size} 点")
        self.capture.display.frame_displayed()
        self.capture.show_frontend(f)
        self.settings.setText(self.show_settings(frame.batch.configuration["settings"]))
        self.capture.sync(frame.batch.configuration["settings"])
        self.last = frame
        self.service.displayed(self.device_id, frame)
        self.export_button.setEnabled(True)

    def showEvent(self, event):
        super().showEvent(event)
        if self.kind:
            self.refresh_plot(self.device_id)
        self.refresh_status()

    def export(self, frame=None):
        frame = self.last if frame is None else frame
        if frame is None or frame.request.channels != (self.channel,) or frame.request.kind != self.kind:
            self.status.setText("没有可导出的当前通道帧")
            return
        self.controller.watch(
            self.service.export_displayed_frame(self.device_id, frame, self.controller.mode),
            lambda path: self.status.setText(str(path)),
        )
