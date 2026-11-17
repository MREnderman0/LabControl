"""The spectrum workspace: one plot, four setting rows, local markers."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from lab_control.application.config import config_from_record
from lab_control.application.controls import DataSource, data_sources
from lab_control.domain.spectrum import SpectrumMarker
from lab_control.instruments.registry import DEVICE_DEFINITIONS
from lab_control.ui.controller import state_label
from lab_control.ui.pages.monitoring.connection import refresh_reconnect_button
from lab_control.ui.pages.monitoring.recording_state import (
    RecordingButton,
    recording_phase,
    recording_save_notice,
    update_recording_frame,
)
from lab_control.ui.pages.monitoring.spectrum_controls import SpectrumControls
from lab_control.ui.pages.monitoring.spectrum_markers import MarkerDialog, SpectrumPlot, frequency_text
from lab_control.ui.pages.monitoring.timing import SpectrumInterval
from lab_control.ui.widgets.device_selector import DeviceSelector
from lab_control.ui.widgets.readouts import ReadoutStrip


class SpectrumMonitoringPage(QWidget):
    def __init__(self, controller, add_card, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("spectrumWorkspace")
        self.controller, self.add_card = controller, add_card
        self.device_id = ""
        self.mode = controller.mode
        self.controls: SpectrumControls | None = None
        self.source: DataSource | None = None
        self.markers: list[SpectrumMarker] = []
        self.marker_key = ""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 12)
        layout.setSpacing(10)
        header = QHBoxLayout()
        self.selector = DeviceSelector(controller, "spectrum_analyzer", self, compact=True)
        header.addWidget(self.selector, 1)
        self.connection = QLabel(self)
        self.connection.setObjectName("sourceConnectionState")
        header.addWidget(self.connection)
        self.start_button = QPushButton("开始监测", self)
        self.start_button.clicked.connect(self.toggle_monitoring)
        header.addWidget(self.start_button)
        layout.addLayout(header)
        self.empty = QLabel("请先从总览连接频谱仪，再选择设备。", self)
        layout.addWidget(self.empty)
        self.splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.splitter.setChildrenCollapsible(False)
        chart = self.chart = QFrame(self.splitter)
        chart.setObjectName("spectrumChart")
        chart.setMinimumWidth(280)
        plot_layout = QVBoxLayout(chart)
        plot_layout.setContentsMargins(10, 10, 10, 10)
        toolbar = QHBoxLayout()
        title = QLabel("频谱", chart)
        title.setObjectName("spectrumChartTitle")
        toolbar.addWidget(title)
        toolbar.addStretch()
        self.record_button = RecordingButton(chart)
        self.record_button.clicked.connect(self.record)
        toolbar.addWidget(self.record_button)
        self.interval = SpectrumInterval(controller, chart)
        self.interval.setFixedWidth(self.record_button.width())
        toolbar.addWidget(self.interval)
        plot_layout.addLayout(toolbar)
        self.readouts = ReadoutStrip(
            {"center": "中心频率", "span": "扫宽", "bw": "RBW / VBW", "ref": "参考电平", "marker": "标记"},
            highlight=("marker",),
        )
        plot_layout.addWidget(self.readouts)
        self.plot = SpectrumPlot(chart)
        self.plot.add_frequency.connect(self.add_marker_at)
        self.plot.save_frame.connect(self.save)
        self.plot.edit_marker.connect(self.edit_marker)
        self.plot.readings_changed.connect(self.update_marker_list)
        self.plot.readings_changed.connect(self.update_readouts)
        plot_layout.addWidget(self.plot, 1)
        self.status = QLabel("监测未开始", chart)
        self.status.setObjectName("mutedText")
        self.status.setWordWrap(True)
        footer = QHBoxLayout()
        footer.addWidget(self.status, 1)
        self.actual = QLabel("实际 — s/帧")
        self.actual.setObjectName("rangeHint")
        footer.addWidget(self.actual)
        plot_layout.addLayout(footer)
        self.splitter.addWidget(chart)
        self.right = QWidget(self.splitter)
        self.right.setMinimumWidth(360)
        self.right.setMaximumWidth(430)
        self.right_layout = QVBoxLayout(self.right)
        self.right_layout.setContentsMargins(8, 0, 0, 0)
        self.splitter.addWidget(self.right)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 0)
        self.splitter.setSizes([800, 400])
        layout.addWidget(self.splitter, 1)
        self.marker_list: QListWidget | None = None
        self.selector.selected.connect(self.select_device)
        controller.refreshed.connect(self.refresh)
        controller.frame_ready.connect(self.refresh_frame)
        self.select_device(self.selector.device_id)

    def select_device(self, device_id: str) -> None:
        if self.controls and self.controls.running:
            return
        if self.device_id == device_id and self.mode == self.controller.mode and self.controls:
            return
        self.device_id, self.mode = device_id, self.controller.mode
        self.interval.bind(device_id, self.mode)
        self.plot.reset()
        self.marker_list = None
        if self.controls:
            self.right_layout.removeWidget(self.controls)
            self.controls.hide()
            self.controls.deleteLater()
            self.controls = None
        self.source = None
        self.markers = []
        self.splitter.setVisible(bool(device_id))
        self.empty.setVisible(not device_id)
        if device_id:
            config = config_from_record(self.controller.records[device_id])
            definition = DEVICE_DEFINITIONS.get(config.adapter)
            if definition:
                self.plot.trace_view.default_dbm = definition.spectrum_view_dbm
            self.source = next((s for s in data_sources(config) if s.kind == "spectrum"), None)
            self.controls = SpectrumControls(self.controller, device_id, self.right)
            self.right_layout.addWidget(self.controls)
            self.controls.busy_changed.connect(lambda busy: self.selector.setEnabled(not busy))
            row = QHBoxLayout()
            label = QLabel("软件标记", self.controls)
            label.setObjectName("spectrumChartTitle")
            row.addWidget(label)
            row.addStretch()
            add = QPushButton("＋ 添加标记", self.controls)
            add.clicked.connect(self.add_marker)
            row.addWidget(add)
            self.controls.marker_slot.addLayout(row)
            self.marker_list = QListWidget(self.controls)
            self.marker_list.setObjectName("spectrumMarkerList")
            self.marker_list.setMinimumHeight(92)
            self.marker_list.setMaximumHeight(130)
            self.marker_list.itemDoubleClicked.connect(
                lambda item: self.edit_marker(item.data(Qt.ItemDataRole.UserRole))
            )
            self.controls.marker_slot.addWidget(self.marker_list)
            hint = QLabel("双击标记可编辑 · 仅作用于软件曲线", self.controls)
            hint.setObjectName("rangeHint")
            self.controls.marker_slot.addWidget(hint)
            self.marker_key = f"spectrum-markers/{self.mode}/{device_id}"
            stored = self.controller.runtime.repository.load_workspace(self.marker_key, [])
            if isinstance(stored, list):
                for value in stored:
                    try:
                        marker = SpectrumMarker(**value)
                        if marker.number not in {m.number for m in self.markers}:
                            self.markers.append(marker)
                    except (ValueError, TypeError):
                        continue
            self.plot.set_markers(self.markers)
        if self.isVisible():
            self.controller.focus_settings(device_id)
        self.refresh()
        self.refresh_frame(device_id)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self.controller.focus_settings(self.device_id)
        self.refresh_frame(self.device_id)

    def hideEvent(self, event) -> None:  # noqa: N802
        super().hideEvent(event)
        if self.controller.sync_device == self.device_id:
            self.controller.focus_settings("")

    def focus_card(self, spec: dict) -> None:
        self.selector.select(spec["device_id"])

    def toggle_monitoring(self) -> None:
        if self.controller.reconnect_if_needed(self.device_id):
            return
        if not self.source or not self.device_id:
            return
        service = self.controller.runtime.monitoring
        if service.snapshots().get(self.device_id, {}).get("running"):
            self.controller.watch(service.stop(self.device_id))
        else:
            try:
                self.controller.ensure_applicable(self.device_id)
                # Passive reads: never INIT, change trigger mode, or start a physical sweep.
                self.interval.interpretText()
                request = self.source.request((1,))
                self.controller.start_monitoring(
                    self.device_id,
                    (request,),
                    **self.interval.options(),
                    mode=self.mode,
                )
            except Exception as exc:
                self.controller.message.emit(str(exc))

    def record(self) -> None:
        service = self.controller.runtime.monitoring
        task = service.snapshots().get(self.device_id, {})
        try:
            self.interval.interpretText()
            self.controller.set_recording(self.device_id, not task.get("recording"), mode=self.mode)
            self.refresh()
        except Exception as exc:
            self.controller.message.emit(str(exc))

    def save(self, frame=None) -> None:
        if frame is None:
            frame = self.plot.displayed_frame
        if frame is not None:
            try:
                self.controller.watch(
                    self.controller.runtime.monitoring.export_displayed_frame(
                        self.device_id, frame, self.mode
                    ),
                    lambda path: self.controller.message.emit(f"原始数据已保存 · 分析 CSV 已完成：{path}"),
                )
                self.refresh()
            except Exception as exc:
                self.controller.message.emit(str(exc))

    def pin(self) -> None:
        if self.source:
            self.add_card(self.device_id, self.source.key, (1,))

    def add_marker(self) -> None:
        current = self.controls.current() if self.controls else {}
        self.add_marker_at(current.get("center_frequency", 0))

    def add_marker_at(self, frequency: float) -> None:
        current = self.controls.current() if self.controls else {}
        marker = SpectrumMarker(
            max((m.number for m in self.markers), default=0) + 1,
            frequency,
            half_width=max(1.0, current.get("span", 20) * 0.05),
        )
        self.marker_dialog(marker, creating=True)

    def edit_marker(self, number: int) -> None:
        marker = next((m for m in self.markers if m.number == number), None)
        if marker:
            self.marker_dialog(marker)

    def marker_dialog(self, marker: SpectrumMarker, *, creating=False) -> None:
        dialog = MarkerDialog(marker, self, creating=creating)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.markers = [m for m in self.markers if m.number != marker.number]
            if not dialog.deleted:
                self.markers.append(dialog.marker)
            self.markers.sort(key=lambda m: m.number)
            self.controller.runtime.repository.save_workspace(
                self.marker_key, [asdict(m) for m in self.markers]
            )
            self.plot.set_markers(self.markers)
        dialog.deleteLater()

    def update_readouts(self) -> None:
        """Instrument settings of the displayed frame, plus the first marker reading."""
        frame = self.plot.displayed_frame
        # Verified readback values from the control panel; the displayed frame's own settings win.
        config = dict(self.controls.current()) if self.controls else {}
        if frame:
            config.update({k: v for k, v in frame.batch.configuration.items() if v is not None})

        def number(key):
            value = config.get(key)
            return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None

        start, stop = number("start_frequency"), number("stop_frequency")
        center, span = number("center_frequency"), number("span")
        if start is not None and stop is not None:
            center = (start + stop) / 2 if center is None else center
            span = stop - start if span is None else span
        self.readouts.set("center", frequency_text(center) if center is not None else "—")
        self.readouts.set("span", frequency_text(span) if span is not None else "—")
        rbw, vbw = number("rbw"), number("vbw")
        self.readouts.set(
            "bw",
            " / ".join(frequency_text(v) if v is not None else "—" for v in (rbw, vbw))
            if rbw is not None or vbw is not None
            else "—",
        )
        reference = number("reference_level")
        unit = (
            (frame.batch.measurements[0].metadata.get("spectrum_display") or {}).get("unit")
            if frame and frame.batch.measurements
            else None
        )
        self.readouts.set(
            "ref", f"{reference:g} {unit or 'dBm'}".replace("-", "−") if reference is not None else "—"
        )
        marker = self.markers[0] if self.markers else None
        reading = self.plot.readings.get(marker.number) if marker else None
        if marker and reading and reading.amplitude is not None:
            position = reading.position if reading.position is not None else marker.frequency
            self.readouts.set(
                "marker",
                f"{reading.amplitude:.2f} {self.plot.amplitude_unit}".replace("-", "−"),
                f"M{marker.number} {'寻峰' if marker.tracking else '固定'} · {frequency_text(position)}",
            )
        else:
            self.readouts.set("marker", "—", f"M{marker.number}" if marker else "标记")

    def update_marker_list(self) -> None:
        if self.marker_list is None:
            return
        for i, marker in enumerate(self.markers):
            reading = self.plot.readings.get(marker.number)
            text = f"M{marker.number}  {'寻峰' if marker.tracking else '固定'}  "
            text += frequency_text(
                reading.position if reading and reading.position is not None else marker.frequency
            )
            text += (
                f"\n{reading.amplitude:.8g} {self.plot.amplitude_unit}"
                if reading and reading.amplitude is not None
                else "\n" + (reading.status if reading else "无数据")
            )
            item = self.marker_list.item(i)
            if item is None:
                item = QListWidgetItem(self.marker_list)
            if item.text() != text:
                item.setText(text)
            item.setData(Qt.ItemDataRole.UserRole, marker.number)
            item.setToolTip(
                f"中心：{frequency_text(marker.frequency)} · 搜索 ±{frequency_text(marker.half_width)}"
            )
        while self.marker_list.count() > len(self.markers):
            self.marker_list.takeItem(self.marker_list.count() - 1)

    def refresh(self) -> None:
        if self.device_id != self.selector.device_id and not (self.controls and self.controls.running):
            self.select_device(self.selector.device_id)
            return
        if self.mode != self.controller.mode:
            self.select_device(self.selector.device_id)
            return
        state = self.controller.states.get(self.device_id, {})
        service = self.controller.runtime.monitoring
        task = service.snapshots().get(self.device_id, {})
        actual = task.get("frame_interval")
        self.actual.setText(f"实际 {actual:.2f} s/帧" if actual is not None else "实际 — s/帧")
        busy = bool(self.controls and self.controls.running)
        connected = state.get("connectionState") in {"connected", "busy"}
        self.connection.setText(
            "已连接" if connected else state_label(state.get("connectionState", "disconnected"))
        )
        self.start_button.setText("暂停监测" if task.get("running") else "开始监测")
        self.start_button.setEnabled(
            bool(
                (connected or task.get("running"))
                and not busy
                and not (task.get("pending") and not task.get("running"))
            )
        )
        refresh_reconnect_button(self.start_button, self.controller, self.device_id, task, busy=busy)
        self.record_button.refresh(
            task, connected=connected, busy=busy, locked=bool(state.get("controlsLocked"))
        )
        update_recording_frame(self.chart, task)
        frame = (
            service.latest(self.device_id, self.source.kind, self.source.quantity) if self.source else None
        )
        self.plot.save_enabled = not task.get("display_saving") and not task.get("capture_saving")
        if frame is None and self.plot.trace is not None:
            self.plot.trace = None
            self.plot.meta.setText("等待当前配置的新频谱 · 图中保留旧曲线")
            self.plot.update_markers()
        text = (
            task.get("error")
            or task.get("notice")
            or task.get("recording_notice")
            or recording_save_notice(task)
            or (
                "设置中 · 曲线读取暂停"
                if busy
                else "记录收尾中 · 正在排空已接收数据"
                if recording_phase(task) == "draining"
                else "正在记录原始数据"
                if task.get("recording")
                else "设备已断开"
                if not connected
                else "监测中"
                if task.get("running")
                else "监测已暂停"
                if self.plot.displayed_frame
                else "监测未开始"
            )
        )
        detail = text
        text = detail.partition("\n目录：")[0]
        if not connected and self.device_id and "断开" not in text:
            text = "设备已断开 · " + text
        elif not task.get("running") and self.plot.displayed_frame and "暂停" not in text:
            text = "监测已暂停 · " + text
        displayed = self.plot.displayed_frame
        if displayed and (not task.get("running") or not connected or frame is None):
            stamp = datetime.fromisoformat(displayed.batch.timestamp).astimezone()
            text += f" · 最后一帧 {stamp:%H:%M:%S}"
        if displayed and self.plot.trace_view.panel_y is None:
            text += " · 仪器刻度暂不可用"
        self.status.setToolTip(detail)
        self.status.setText(text)
        self.update_readouts()

    def refresh_frame(self, device_id: str) -> None:
        if device_id != self.device_id or not self.isVisible() or not self.source:
            return
        service = self.controller.runtime.monitoring
        frame = service.latest(device_id, self.source.kind, self.source.quantity)
        if frame:
            self.plot.show_frame(frame, self.source, (1,))
            service.displayed(device_id, frame)
            self.update_readouts()
