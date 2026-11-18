"""Role workspaces over a single Moku session. Navigation performs no device I/O."""

from math import isfinite

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMessageBox,
    QPushButton,
    QScrollArea,
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
from lab_control.ui.controller import display_connection_state, state_label
from lab_control.ui.kit import Card, Dot, ReadoutStrip, style_button
from lab_control.ui.pages.monitoring.frame import page_layout, show_state
from lab_control.ui.pages.monitoring.oscilloscope_plot import OscilloscopePlot
from lab_control.ui.pages.monitoring.spectrum import SpectrumMarkerActions
from lab_control.ui.pages.monitoring.spectrum_markers import SpectrumPlot
from lab_control.ui.widgets.device_selector import DeviceSelector
from lab_control.ui.widgets.workspace_combo import WorkspaceComboBox

from .adapter import LABELS
from .protocol import CHANNELS
from .source_card import GeneratorCard


class RolePage(SpectrumMarkerActions, QWidget):
    def __init__(self, controller, device_id, role):
        super().__init__()
        self.controller, self.device_id, self.role = controller, device_id, role
        self.service = controller.runtime.monitoring
        self.kind = {"oscilloscope": "waveform", "spectrum_analyzer": "spectrum"}.get(role, "")
        self._start_ticket = None
        self.last: MonitorFrame | None = None
        self.status = QLabel("监测未开始" if self.kind else "")
        self.status.setObjectName("mutedText")
        self.status.setWordWrap(True)
        self.hint = QLabel()
        self.hint.setObjectName("rangeHint")
        self.hint.setWordWrap(True)
        layout = QVBoxLayout(self)
        page_layout(layout)
        self.header_grid = QGridLayout()
        self.header_grid.setContentsMargins(0, 0, 0, 0)
        self.header_grid.setColumnStretch(0, 1)
        header = QHBoxLayout()
        self.selector = DeviceSelector(controller, role, self, compact=True, bound_device=device_id)
        header.addWidget(self.selector, 1)
        self.connection = QLabel()
        self.connection.setObjectName("sourceConnectionState")
        self.connection.hide()
        header.addWidget(self.connection)
        header.addWidget(QLabel("通道" if self.kind else "定位通道"))
        self.channels = WorkspaceComboBox()
        for ch in CHANNELS:
            self.channels.addItem(f"ch{ch}", ch)
        header.addWidget(self.channels)
        self.header_grid.addLayout(header, 0, 0)
        layout.addLayout(self.header_grid)
        if self.kind:
            self.header_actions = QWidget()
            actions = QHBoxLayout(self.header_actions)
            actions.setContentsMargins(0, 0, 0, 0)
            self._build_monitor(layout, actions)
            self.narrow_header = None
            self._arrange_header()
        else:
            self._build_generator(layout)
        controller.refreshed.connect(self.refresh_status)

    def _build_generator(self, layout):
        self.card_scroll = QScrollArea()
        self.card_scroll.setWidgetResizable(True)
        body = QWidget()
        self.grid = QGridLayout(body)
        self.grid.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.grid.setSpacing(12)
        self.cards = {ch: GeneratorCard(self, ch) for ch in CHANNELS}
        self.columns = 0
        self._reflow()
        self.card_scroll.setWidget(body)
        layout.addWidget(self.card_scroll, 1)
        layout.addWidget(self.status)
        layout.addWidget(self.hint)
        self.channels.currentIndexChanged.connect(
            lambda: self.card_scroll.ensureWidgetVisible(self.cards[self.channel])
        )

    def _reflow(self):
        columns = 2 if self.width() >= 780 else 1
        if columns == self.columns:
            return
        self.columns = columns
        for index, card in enumerate(self.cards.values()):
            self.grid.addWidget(card, index // columns, index % columns)
        self.grid.setColumnStretch(0, 1)
        self.grid.setColumnStretch(1, int(columns == 2))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "cards"):
            self._reflow()
        if hasattr(self, "narrow_header"):
            self._arrange_header()

    def _arrange_header(self):
        narrow = self.width() < 780
        if narrow != self.narrow_header:
            self.narrow_header = narrow
            self.header_grid.addWidget(
                self.header_actions, int(narrow), 0 if narrow else 1, alignment=Qt.AlignmentFlag.AlignRight
            )

    def _build_monitor(self, layout, header):
        self.monitor_button = self._button(header, "开始监测", lambda: self.start(False), icon="play")
        self.stop_button = self._button(header, "停止本面板", self.stop, icon="stop")
        self.record_button = self._button(header, "逐帧记录", lambda: self.start(True), icon="record")
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        chart = Card(padding=(12, 8, 12, 8))
        chart.setMinimumWidth(280)
        config = ConnectionConfig(**self.controller.records[self.device_id]["config"])
        self.source = next(s for s in data_sources(config) if s.kind == self.kind)
        toolbar = chart.header(self.source.label)
        toolbar.addStretch()
        self.export_button = self._button(toolbar, "导出当前帧", self.export)
        readout_labels = (
            {"peak": "峰峰值", "level": "平均电压"}
            if self.kind == "waveform"
            else {"peak": "主峰频率", "level": "峰值电平"}
        )
        self.readouts = ReadoutStrip({**readout_labels, "samples": "帧点数"}, highlight=("peak",))
        chart.body.addWidget(self.readouts)
        self.plot = OscilloscopePlot() if self.kind == "waveform" else SpectrumPlot()
        chart.body.addWidget(self.plot, 1)
        footer = chart.footer()
        self.dot = Dot("idle", 7)
        footer.addWidget(self.dot)
        self.status.setObjectName("chartStatus")
        footer.addWidget(self.status, 1)
        self.actual = QLabel("实际 — s/帧")
        self.actual.setObjectName("rangeHint")
        footer.addWidget(self.actual)
        self.splitter.addWidget(chart)

        panel = Card("采集参数")
        side = panel.body
        self.settings = QLabel("等待首帧")
        self.settings.setWordWrap(True)
        side.addWidget(self.settings)
        front_title = QLabel("共享模拟前端")
        front_title.setObjectName("sectionTitle")
        side.addWidget(front_title)
        fields = QFormLayout()
        side.addLayout(fields)
        self.impedance, self.coupling, self.attenuation = (WorkspaceComboBox() for _ in range(3))
        for label, box, options in (
            ("输入阻抗", self.impedance, ("50Ohm", "1MOhm")),
            ("输入耦合", self.coupling, ("DC", "AC")),
            ("输入衰减", self.attenuation, ("0dB", "20dB", "40dB")),
        ):
            box.addItems(options)
            fields.addRow(label, box)
        self.readback = QLabel("未读回")
        self.readback.setWordWrap(True)
        side.addWidget(self.readback)
        self.apply_front = self._button(panel.footer(), "应用前端", self.apply_frontend, variant="primary")
        if isinstance(self.plot, SpectrumPlot):
            self.marker_plot = self.plot
            self.markers = []
            self.marker_list = QListWidget()
            self.marker_list.setObjectName("spectrumMarkerList")
            self.marker_list.setMaximumHeight(120)
            self.marker_list.itemDoubleClicked.connect(
                lambda item: self.edit_marker(item.data(Qt.ItemDataRole.UserRole))
            )
            self._button(side, "＋ 添加标记", self.add_center_marker)
            side.addWidget(self.marker_list)
            self.marker_plot.add_frequency.connect(self.add_marker_at)
            self.marker_plot.edit_marker.connect(self.edit_marker)
            self.marker_plot.save_frame.connect(self.export)
            self.marker_plot.readings_changed.connect(self.update_marker_list)
        side.addStretch()
        side.addWidget(self.hint)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setMinimumWidth(245)
        scroll.setMaximumWidth(430)
        scroll.setWidget(panel)
        self.splitter.addWidget(scroll)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setSizes([800, 330])
        layout.addWidget(self.splitter, 1)
        self.channels.currentIndexChanged.connect(self.clear_curve)
        self.clear_curve()
        self.controller.frame_ready.connect(self.refresh_plot)

    def apply_frontend(self):
        self.command(
            "shared_frontend",
            {
                "impedance": self.impedance.currentText(),
                "coupling": self.coupling.currentText(),
                "attenuation": self.attenuation.currentText(),
            },
            f"修改 ch{self.channel} 的阻抗、耦合和衰减？两个测量面板共用这一物理前端。",
        )

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

    def focus_card(self, spec):
        channels = spec.get("channels", [])
        if spec.get("device_id") == self.device_id and channels and self.channels.isEnabled():
            index = self.channels.findData(channels[0])
            if index >= 0:
                self.channels.setCurrentIndex(index)

    def _button(self, row, title, action, *, icon="", variant=""):
        button = QPushButton(title)
        style_button(button, variant, icon)

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

    def command(self, op, params, message, *, channel=None):
        ch = self.channel if channel is None else channel
        if not self._confirm(message):
            return
        try:
            self.controller.apply(
                self.device_id,
                DeviceCommand(op, params, ch, confirmed=True),
                lambda r: self.status.setText(
                    f"CH {ch} · 设备已确认命令" if r.outcome == "success" else r.error
                ),
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
        self.connection.setText(state_label(display_connection_state(state)))
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
            for card in self.cards.values():
                card.setEnabled(writable)
            return
        snap = self.service.snapshots().get(self.device_id, {})
        pending = self._start_ticket is not None
        running = bool(snap.get("running") and self.kind in snap.get("kinds", ()))
        recording = self.kind in snap.get("recording_kinds", ())
        show_state(self.dot, {**snap, "running": running, "recording": recording}, connected)
        self.monitor_button.setEnabled(connected and not pending and not running)
        self.record_button.setEnabled(connected and not pending and not recording)
        self.record_button.setText("记录中" if recording else "逐帧记录")
        self.stop_button.setEnabled(pending or running)
        self.channels.setEnabled(not (pending or running))
        self.apply_front.setEnabled(writable and not pending)
        self.export_button.setEnabled(self.last is not None and not snap.get("display_saving"))
        actual = snap.get("frame_interval")
        self.actual.setText(f"整机实际 {actual:.2f} s/帧" if actual is not None else "实际 — s/帧")
        if self.kind == "spectrum":
            self.marker_plot.save_enabled = self.export_button.isEnabled()
        if self.last is not None and self.service.latest(self.device_id, self.kind) is None:
            self.clear_curve()
        note = snap.get("error") or snap.get("recording_notice") or snap.get("notice")
        if note:
            self.status.setText(note)

    def clear_curve(self):
        self.plot.reset()
        for key in self.readouts.cells:
            self.readouts.set(key, "—")
        self.readback.setText("未读回")
        self.settings.setText("等待首帧")
        self.last = None
        self.export_button.setEnabled(False)
        self.frontend_draft_started = False
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
            raw = settings.get(group, {}).get(key)
            return engineering(raw, unit) if isinstance(raw, (int, float)) and isfinite(raw) else "—"

        if self.kind == "waveform":
            low, high = (value("timebase", k, "s") for k in ("timebase1", "timebase2"))
            return f"时窗　{low} ～ {high}\n采样率　{value('samplerate', 'sample_rate', 'Sa/s')}"
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
        if self.kind == "waveform":
            self.readouts.set("peak", engineering(float(m.values.max() - m.values.min()), "V"))
            self.readouts.set("level", engineering(float(m.values.mean()), "V"))
        else:
            peak = int(m.values.argmax())
            self.readouts.set("peak", engineering(float(m.axis[peak]), "Hz"))
            self.readouts.set("level", f"{m.values[peak]:.2f} {m.unit}")
        self.readouts.set("samples", str(m.values.size))
        self.readback.setText(
            "读回：" + " / ".join(str(f.get(k, "—")) for k in ("impedance", "coupling", "attenuation"))
        )
        self.settings.setText(self.show_settings(frame.batch.configuration["settings"]))
        if not self.frontend_draft_started:
            for key, box in (
                ("impedance", self.impedance),
                ("coupling", self.coupling),
                ("attenuation", self.attenuation),
            ):
                idx = box.findText(str(f.get(key, "")), Qt.MatchFlag.MatchFixedString)
                if idx >= 0:
                    box.setCurrentIndex(idx)
            self.frontend_draft_started = True
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
