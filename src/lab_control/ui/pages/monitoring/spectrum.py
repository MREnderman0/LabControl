"""The spectrum workspace: one plot, four setting rows, local markers."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime

from PySide6.QtCore import QRect, QSize, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSplitter,
    QStyle,
    QStyledItemDelegate,
    QVBoxLayout,
    QWidget,
)

from lab_control.application.config import config_from_record
from lab_control.application.controls import DataSource, data_sources
from lab_control.domain.spectrum import SpectrumMarker
from lab_control.domain.spectrum_display import amplitude_unit
from lab_control.instruments.registry import DEVICE_DEFINITIONS
from lab_control.ui.controller import WorkspaceController, state_label
from lab_control.ui.kit import Dot, EmptyState, IconButton, PageHeader, style_button
from lab_control.ui.kit.readouts import ReadoutStrip
from lab_control.ui.pages.monitoring.connection import refresh_reconnect_button
from lab_control.ui.pages.monitoring.recording_state import (
    RecordingButton,
    recording_phase,
    recording_save_notice,
    update_recording_frame,
)
from lab_control.ui.pages.monitoring.spectrum_controls import SpectrumControls
from lab_control.ui.pages.monitoring.spectrum_markers import (
    MARKER_COLORS,
    MarkerDialog,
    SpectrumPlot,
    frequency_text,
)
from lab_control.ui.pages.monitoring.timing import MonitoringInterval, actual_interval_text
from lab_control.ui.styles import tokens as tk
from lab_control.ui.widgets.device_selector import DeviceSelector


class SpectrumMarkerDelegate(QStyledItemDelegate):
    """Paints each spectrum marker as a 3-column Graphite card."""

    def sizeHint(self, option, index) -> QSize:  # noqa: N802
        return QSize(0, 52)

    def paint(self, painter: QPainter, option, index) -> None:  # noqa: N802
        data = index.data(Qt.ItemDataRole.UserRole + 1)
        if not data:
            super().paint(painter, option, index)
            return
        number, mode_text, center_text, amp_text, freq_text, has_value = data
        color_hex = MARKER_COLORS[(number - 1) % len(MARKER_COLORS)]
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = option.rect.adjusted(0, 0, 0, -6)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        bg = QColor(tk.ACCENT_SOFT if selected else (tk.HOVER if hovered else tk.SURFACE_2))
        if selected:
            border = QColor(tk.ACCENT)
            border.setAlpha(140)
        else:
            border = QColor(tk.BORDER_STRONG if hovered else tk.BORDER)
        painter.setBrush(bg)
        painter.setPen(QPen(border, 1))
        painter.drawRoundedRect(rect.adjusted(0, 0, -1, -1), 7, 7)

        badge_rect = QRect(rect.left() + 10, rect.center().y() - 11, 28, 22)
        badge_bg = QColor(color_hex)
        badge_bg.setAlpha(42)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(badge_bg)
        painter.drawRoundedRect(badge_rect, 5, 5)

        badge_font = QFont(option.font)
        badge_font.setPixelSize(11)
        badge_font.setWeight(QFont.Weight.Bold)
        painter.setFont(badge_font)
        painter.setPen(QColor(color_hex))
        painter.drawText(badge_rect, Qt.AlignmentFlag.AlignCenter, f"M{number}")

        amp_font = QFont(option.font)
        amp_font.setFamilies(list(tk.FONT_MONO))
        amp_font.setPixelSize(12)
        amp_font.setWeight(QFont.Weight.DemiBold)
        freq_font = QFont(option.font)
        freq_font.setFamilies(list(tk.FONT_MONO))
        freq_font.setPixelSize(11)
        amp_metrics = QFontMetrics(amp_font)
        freq_metrics = QFontMetrics(freq_font)
        right_w = min(
            132,
            max(amp_metrics.horizontalAdvance(amp_text), freq_metrics.horizontalAdvance(freq_text)) + 6,
        )

        top_y = rect.top() + 7
        row_h = 16
        right_x = rect.right() - 10 - right_w
        mid_x = badge_rect.right() + 10
        mid_w = max(40, right_x - mid_x - 8)

        mode_font = QFont(option.font)
        mode_font.setPixelSize(12)
        mode_font.setWeight(QFont.Weight.DemiBold)
        center_font = QFont(option.font)
        center_font.setPixelSize(11)
        painter.setFont(mode_font)
        painter.setPen(QColor(tk.TEXT))
        painter.drawText(
            QRect(mid_x, top_y, mid_w, row_h),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            QFontMetrics(mode_font).elidedText(mode_text, Qt.TextElideMode.ElideRight, mid_w),
        )
        painter.setFont(center_font)
        painter.setPen(QColor(tk.TEXT_3))
        painter.drawText(
            QRect(mid_x, top_y + row_h, mid_w, row_h),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            QFontMetrics(center_font).elidedText(center_text, Qt.TextElideMode.ElideRight, mid_w),
        )

        painter.setFont(amp_font)
        painter.setPen(QColor(tk.TEXT if has_value else tk.TEXT_3))
        painter.drawText(
            QRect(right_x, top_y, right_w, row_h),
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            amp_metrics.elidedText(amp_text, Qt.TextElideMode.ElideRight, right_w),
        )
        painter.setFont(freq_font)
        painter.setPen(QColor(tk.TEXT_3))
        painter.drawText(
            QRect(right_x, top_y + row_h, right_w, row_h),
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            freq_metrics.elidedText(freq_text, Qt.TextElideMode.ElideRight, right_w),
        )
        painter.restore()


class SpectrumMarkerActions:
    controller: WorkspaceController
    markers: list[SpectrumMarker]
    marker_key: str
    marker_list: QListWidget | None
    marker_empty: QLabel | None
    marker_plot: SpectrumPlot

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
            self.marker_plot.set_markers(self.markers)
        dialog.deleteLater()

    def update_marker_list(self) -> None:
        if self.marker_list is None:
            return
        for i, marker in enumerate(self.markers):
            reading = self.marker_plot.readings.get(marker.number)
            has_value = bool(reading and reading.amplitude is not None)
            mode_text = f"寻峰 · ±{frequency_text(marker.half_width)}" if marker.tracking else "固定频率"
            center_text = f"中心 {frequency_text(marker.frequency)}"
            if has_value:
                assert reading is not None and reading.amplitude is not None
                amp_text = f"{reading.amplitude:.8g} {self.marker_plot.amplitude_unit}".replace(
                    "-", "−"
                ).strip()
                freq_text = frequency_text(
                    reading.position if reading.position is not None else marker.frequency
                )
            else:
                amp_text = reading.status if reading else "无数据"
                freq_text = "—"
            plain = f"M{marker.number}  {'寻峰' if marker.tracking else '固定'}  {freq_text}\n{amp_text}"
            item = self.marker_list.item(i)
            if item is None:
                item = QListWidgetItem(self.marker_list)
            if item.text() != plain:
                item.setText(plain)
            item.setData(Qt.ItemDataRole.UserRole, marker.number)
            item.setData(
                Qt.ItemDataRole.UserRole + 1,
                (marker.number, mode_text, center_text, amp_text, freq_text, has_value),
            )
            item.setToolTip(
                f"中心：{frequency_text(marker.frequency)} · 搜索 ±{frequency_text(marker.half_width)}"
            )
        while self.marker_list.count() > len(self.markers):
            self.marker_list.takeItem(self.marker_list.count() - 1)
        if getattr(self, "marker_empty", None) is not None:
            assert self.marker_empty is not None
            self.marker_empty.setVisible(not self.markers)
        self.marker_list.setFixedHeight(min(170, len(self.markers) * 52) if self.markers else 0)


class SpectrumMonitoringPage(SpectrumMarkerActions, QWidget):
    def __init__(self, controller, add_card=None, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("spectrumWorkspace")
        self.controller = controller
        self.device_id = ""
        self.mode = controller.mode
        self.controls: SpectrumControls | None = None
        self.source: DataSource | None = None
        self.markers: list[SpectrumMarker] = []
        self.marker_key = ""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(14)
        header = PageHeader(self)
        self.selector = DeviceSelector(controller, "spectrum_analyzer", self, compact=True)
        header.add_left(self.selector, 1)
        self.connection = QLabel(self)
        self.connection.setObjectName("sourceConnectionState")
        self.connection.hide()
        self.interval = MonitoringInterval(controller, self)
        header.add_right(self.interval)
        self.start_button = QPushButton("开始监测", self)
        style_button(self.start_button, "", "play")
        self.start_button.clicked.connect(self.toggle_monitoring)
        header.add_right(self.start_button)
        self.record_button = RecordingButton(self)
        self.record_button.clicked.connect(self.record)
        header.add_right(self.record_button)
        layout.addWidget(header)
        self.empty = EmptyState("未选择频谱仪", "请先在首页连接频谱仪，再从上方选择设备。", "spec", self)
        layout.addWidget(self.empty, 1)
        self.splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.splitter.setChildrenCollapsible(False)
        chart = self.chart = QFrame(self.splitter)
        chart.setObjectName("spectrumChart")
        chart.setMinimumWidth(280)
        plot_layout = QVBoxLayout(chart)
        plot_layout.setContentsMargins(0, 0, 0, 0)
        plot_layout.setSpacing(0)
        head = QFrame(chart)
        head.setObjectName("cardHead")
        toolbar = QHBoxLayout(head)
        toolbar.setContentsMargins(0, 0, 10, 0)
        toolbar.setSpacing(4)
        self.readouts = ReadoutStrip(
            {"center": "中心频率", "span": "扫宽", "bw": "RBW / VBW", "ref": "参考电平", "marker": "标记"},
            highlight=("marker",),
            embedded=True,
        )
        toolbar.addWidget(self.readouts, 1)
        self.fit_button = IconButton("updown", "适应数据（一次）：按当前曲线缩放纵轴")
        toolbar.addWidget(self.fit_button)
        self.save_button = IconButton("download", "保存当前显示帧（原始数据 + 分析 CSV）")
        self.save_button.clicked.connect(lambda: self.save())
        toolbar.addWidget(self.save_button)
        plot_layout.addWidget(head)
        self.plot = SpectrumPlot(chart)
        self.fit_button.clicked.connect(self.plot.fit_current)
        self.marker_plot = self.plot
        self.plot.add_frequency.connect(self.add_marker_at)
        self.plot.save_frame.connect(self.save)
        self.plot.edit_marker.connect(self.edit_marker)
        self.plot.readings_changed.connect(self.update_marker_list)
        self.plot.readings_changed.connect(self.update_readouts)
        body = QVBoxLayout()
        body.setContentsMargins(10, 10, 12, 6)
        body.addWidget(self.plot, 1)
        plot_layout.addLayout(body, 1)
        foot = QFrame(chart)
        foot.setObjectName("cardFoot")
        footer = QHBoxLayout(foot)
        footer.setContentsMargins(14, 8, 16, 8)
        footer.setSpacing(6)
        self.status_dot = Dot("idle", 7)
        footer.addWidget(self.status_dot)
        self.status = QLabel("监测未开始", chart)
        self.status.setObjectName("chartStatus")
        self.status.setWordWrap(True)
        footer.addWidget(self.status, 1)
        self.actual = QLabel("实际 — s/帧")
        self.actual.setObjectName("chartMeta")
        footer.addWidget(self.actual)
        plot_layout.addWidget(foot)
        self.splitter.addWidget(chart)
        self.right = QWidget(self.splitter)
        self.right.setMinimumWidth(340)
        self.right.setMaximumWidth(380)
        self.right_layout = QVBoxLayout(self.right)
        self.right_layout.setContentsMargins(0, 0, 0, 0)
        self.splitter.addWidget(self.right)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 0)
        self.splitter.setSizes([830, 352])
        layout.addWidget(self.splitter, 1)
        self.marker_list: QListWidget | None = None
        self.marker_empty: QLabel | None = None
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
        self.marker_empty = None
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
            label.setObjectName("sectionTitle")
            row.addWidget(label)
            row.addStretch()
            add = QPushButton("添加标记", self.controls)
            style_button(add, "ghost", "plus", "sm")
            add.clicked.connect(self.add_marker)
            row.addWidget(add)
            self.controls.marker_slot.addLayout(row)
            self.marker_empty = QLabel("暂无标记", self.controls)
            self.marker_empty.setObjectName("rangeHint")
            self.controls.marker_slot.addWidget(self.marker_empty)
            self.marker_list = QListWidget(self.controls)
            self.marker_list.setObjectName("spectrumMarkerList")
            self.marker_list.setItemDelegate(SpectrumMarkerDelegate(self.marker_list))
            self.marker_list.setMouseTracking(True)
            self.marker_list.setSpacing(0)
            self.marker_list.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
            self.marker_list.setFixedHeight(0)
            self.marker_list.itemDoubleClicked.connect(
                lambda item: self.edit_marker(item.data(Qt.ItemDataRole.UserRole))
            )
            self.controls.marker_slot.addWidget(self.marker_list)
            hint = QLabel("双击标记可编辑 · 右键曲线在该频率添加", self.controls)
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
            self.controller.save_displayed_frame(self, self.device_id, frame, self.mode)
            self.refresh()

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
        unit = None
        if frame and frame.batch.configuration.get("reference_level") is not None:
            unit = amplitude_unit(frame.batch.configuration.get("amplitude_unit"))
        elif reference is not None and self.controls:
            editor = self.controls.editors.get("reference_level")
            state = self.controller.states.get(self.device_id, {})
            context = (
                (frame.session_id, frame.configuration_version)
                if frame
                else (state.get("sessionId"), state.get("configurationVersion"))
            )
            if editor and editor.sync_token and editor.sync_token[:2] == context:
                unit = amplitude_unit(editor.cap.parameters["value"].unit)
        self.readouts.set(
            "ref", f"{reference:g} {unit}".replace("-", "−") if reference is not None and unit else "—"
        )
        marker = self.markers[0] if self.markers else None
        reading = self.plot.readings.get(marker.number) if marker else None
        if marker and reading and reading.amplitude is not None:
            position = reading.position if reading.position is not None else marker.frequency
            self.readouts.set(
                "marker",
                f"{reading.amplitude:.8g} {self.plot.amplitude_unit}".replace("-", "−"),
                f"M{marker.number} {'寻峰' if marker.tracking else '固定'} · {frequency_text(position)}",
            )
        else:
            self.readouts.set("marker", "—", f"M{marker.number}" if marker else "标记")

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
        self._update_frame_info(task)
        busy = bool(self.controls and self.controls.running)
        connected = state.get("connectionState") in {"connected", "busy"}
        self.connection.setText(
            "已连接" if connected else state_label(state.get("connectionState", "disconnected"))
        )
        self.start_button.setText("暂停监测" if task.get("running") else "开始监测")
        style_button(self.start_button, "", "pause" if task.get("running") else "play")
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
            self.plot.meta.setVisible(True)
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
        self.status_dot.set_status(
            "err"
            if task.get("error") or not connected
            else "err"
            if task.get("recording")
            else "ok"
            if task.get("running")
            else "idle",
            live=bool(task.get("running")),
        )
        self.fit_button.setEnabled(self.plot.displayed_frame is not None)
        self.save_button.setEnabled(self.plot.displayed_frame is not None and self.plot.save_enabled)
        self.update_readouts()

    def _update_frame_info(self, task: dict) -> None:
        clock = self.plot.last_frame_clock
        actual_text = actual_interval_text(task)
        self.actual.setText(f"最新帧 {clock} · {actual_text}" if clock else actual_text)

    def refresh_frame(self, device_id: str) -> None:
        if device_id != self.device_id or not self.isVisible() or not self.source:
            return
        service = self.controller.runtime.monitoring
        frame = service.latest(device_id, self.source.kind, self.source.quantity)
        if frame:
            self.plot.show_frame(frame, self.source, (1,))
            service.displayed(device_id, frame)
            self._update_frame_info(service.snapshots().get(device_id, {}))
            self.fit_button.setEnabled(self.plot.displayed_frame is not None)
            self.save_button.setEnabled(self.plot.displayed_frame is not None and self.plot.save_enabled)
            self.update_readouts()
