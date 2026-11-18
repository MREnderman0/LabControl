"""Three views over a single connected inventory id. Opening a page performs no I/O."""

from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from lab_control.application.monitoring_state import MonitorFrame
from lab_control.domain.instruments import AcquisitionRequest, DeviceCommand
from lab_control.ui.widgets.plot_export import PublicationPlotWidget

from .adapter import LABELS
from .protocol import CHANNELS


class RolePage(QWidget):
    def __init__(self, controller, device_id, role):
        super().__init__()
        self.controller, self.device_id, self.role = controller, device_id, role
        self.service = controller.runtime.monitoring
        self.kind = {"oscilloscope": "waveform", "spectrum_analyzer": "spectrum"}.get(role, "")
        self._start_ticket = None
        self.last: MonitorFrame | None = None
        layout = QVBoxLayout(self)
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
        row = QHBoxLayout()
        layout.addLayout(row)
        if self.kind:
            self.monitor_button = self._button(row, "开始监测", lambda: self.start(False))
            self.record_button = self._button(row, "逐帧记录", lambda: self.start(True))
            self._button(row, "停止本面板", self.stop)
            self._button(row, "导出当前帧", self.export)
            front = QHBoxLayout()
            layout.addLayout(front)
            front.addWidget(QLabel("共享模拟前端"))
            self.impedance, self.coupling, self.attenuation = QComboBox(), QComboBox(), QComboBox()
            for box, choices in (
                (self.impedance, ("50Ohm", "1MOhm")),
                (self.coupling, ("DC", "AC")),
                (self.attenuation, ("0dB", "20dB", "40dB")),
            ):
                box.addItems(choices)
                front.addWidget(box)
            self._button(
                front,
                "应用前端",
                lambda: self.command(
                    "shared_frontend",
                    {
                        "impedance": self.impedance.currentText(),
                        "coupling": self.coupling.currentText(),
                        "attenuation": self.attenuation.currentText(),
                    },
                    f"修改 ch{self.channel} 的阻抗、耦合和衰减；两个测量面板共用这一前端。",
                ),
            )
            self.readback = QLabel("未读回")
            front.addWidget(self.readback)
            front.addStretch()
            self.plot = PublicationPlotWidget()
            self.plot.addLegend()
            layout.addWidget(self.plot, 1)
            self.plot.setLabel(
                "bottom",
                "时间" if self.kind == "waveform" else "频率",
                units="s" if self.kind == "waveform" else "Hz",
            )
            self.plot.setLabel(
                "left",
                "电压" if self.kind == "waveform" else "功率",
                units="V" if self.kind == "waveform" else "dBm",
            )
            self.channels.currentIndexChanged.connect(self.clear_curve)
            controller.frame_ready.connect(self.refresh_plot)
        else:
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
        layout.addWidget(self.hint)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        if not self.kind:
            layout.addStretch()
        controller.refreshed.connect(self.refresh_status)

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

    def command(self, op, params, message):
        if not self._confirm(message):
            return
        self.controller.apply(
            self.device_id,
            DeviceCommand(op, params, self.channel, confirmed=True),
            lambda r: self.status.setText("操作已确认" if r.outcome == "success" else r.error),
            lambda e: self.status.setText(str(e)),
        )

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
        self.monitor_button.setEnabled(not pending)
        self.record_button.setEnabled(not pending)
        self.channels.setEnabled(
            not (pending or (snap.get("running") and self.kind in snap.get("kinds", ())))
        )
        if self.last is not None and self.service.latest(self.device_id, self.kind) is None:
            self.clear_curve()
        note = snap.get("error") or snap.get("recording_notice") or snap.get("notice")
        if note:
            self.status.setText(note)

    def clear_curve(self):
        self.plot.clear()
        self.readback.setText("未读回")
        self.last = None

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
        f = frame.batch.configuration["frontend"]
        self.readback.setText(
            "读回：" + " / ".join(str(f.get(k, "—")) for k in ("impedance", "coupling", "attenuation"))
        )
        self.plot.clear()
        m = frame.batch.measurements[0]
        self.plot.plot(m.axis, m.values, name=f"ch{m.channel}")
        self.last = frame

    def showEvent(self, event):
        super().showEvent(event)
        if self.kind:
            self.refresh_plot(self.device_id)
        self.refresh_status()

    def export(self):
        frame = self.service.latest(self.device_id, self.kind)
        if frame is None or frame.request.channels != (self.channel,) or frame is not self.last:
            self.status.setText("没有可导出的当前通道帧")
            return
        self.controller.watch(
            self.service.export_displayed_frame(self.device_id, frame, self.controller.mode),
            lambda path: self.status.setText(str(path)),
        )
