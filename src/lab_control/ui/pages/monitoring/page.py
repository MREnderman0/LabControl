from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from lab_control.application.catalog import mode_applicable
from lab_control.application.config import config_from_record
from lab_control.application.controls import CATEGORY_LABELS, DataSource, data_sources
from lab_control.instruments.registry import DEVICE_DEFINITIONS
from lab_control.ui.controller import WorkspaceController
from lab_control.ui.kit import EmptyState, IconButton, PageHeader, icons, style_button
from lab_control.ui.pages.device_control.form import ControlForm
from lab_control.ui.pages.monitoring import frame as page_frame
from lab_control.ui.pages.monitoring.channel_selection import instrument_scan_channels
from lab_control.ui.pages.monitoring.connection import refresh_reconnect_button
from lab_control.ui.pages.monitoring.plot import LivePlot
from lab_control.ui.pages.monitoring.recording_state import (
    recording_phase,
    recording_save_notice,
    update_recording_frame,
)
from lab_control.ui.pages.monitoring.scan import ScanMonitoringPage
from lab_control.ui.pages.monitoring.timing import MonitoringInterval, actual_interval_text
from lab_control.ui.styles import tokens as tk
from lab_control.ui.widgets.device_selector import DeviceSelector
from lab_control.ui.widgets.workspace_combo import WorkspaceComboBox


class MonitoringPage(QWidget):
    scan_page: ScanMonitoringPage | None = None

    def __init__(self, controller: WorkspaceController, category: str, add_card=None, parent=None) -> None:
        super().__init__(parent)
        self.controller, self.category = controller, category
        self.sources: tuple[DataSource, ...] = ()
        self.signature = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(14)
        title = self.title = QLabel(CATEGORY_LABELS[category] + "实时监测")
        title.setObjectName("pageTitle")
        title.hide()  # The header breadcrumb names the page.
        layout.addWidget(title)
        self.header = header = PageHeader(self)
        self.selector = DeviceSelector(controller, category, self, compact=True)
        header.add_left(self.selector, 1)
        self.button_layout = buttons = QGridLayout()
        self.button_layout.setContentsMargins(0, 0, 0, 0)
        self.button_layout.setSpacing(8)
        self.buttons: dict[str, QPushButton | IconButton] = {}
        for key, label, callback in (
            ("once", "单次读取", lambda: self.start(True)),
            ("start", "开始监测", self.start),
            ("stop", "停止监测", self.stop),
            ("record", "开始记录", self.record),
        ):
            button = QPushButton(label)
            style_button(
                button,
                "",
                {"once": "single", "start": "play", "stop": "stop", "record": "record"}[key],
            )
            button.setProperty("buttonSize", "compact")
            button.clicked.connect(callback)
            self.buttons[key] = button
        self.timing = self.interval = MonitoringInterval(controller, self)
        self.run_group = QWidget()
        run_layout = QHBoxLayout(self.run_group)
        run_layout.setContentsMargins(0, 0, 0, 0)
        run_layout.setSpacing(8)
        run_layout.addWidget(self.timing)
        run_layout.addWidget(self.buttons["start"])
        self.button_columns = 0
        self.reflow_buttons()
        self.action_panel = QWidget()
        self.action_panel.setLayout(buttons)
        header.add_right(self.action_panel)
        layout.addWidget(header)
        self.empty = EmptyState(
            f"未选择{CATEGORY_LABELS[category]}",
            f"请先在首页连接{CATEGORY_LABELS[category]}，再从上方选择设备。",
            icons.CATEGORY_ICONS.get(category, "chart"),
            self,
        )
        layout.addWidget(self.empty, 1)
        self.splitter = splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.main_area = main = QFrame()
        main.setObjectName("chartCard")
        main_layout = QVBoxLayout(main)
        main_layout.setContentsMargins(14, 12, 14, 12)
        main_layout.setSpacing(8)
        options = QHBoxLayout()
        options.setSpacing(6)
        acquisition = QWidget()
        settings = QVBoxLayout(acquisition)
        settings.setContentsMargins(12, 12, 12, 12)
        settings.setSpacing(10)
        settings.setAlignment(Qt.AlignmentFlag.AlignTop)
        points_row = QHBoxLayout()
        self.source = WorkspaceComboBox()
        self.source.setMinimumWidth(140)
        self.source.setMaximumWidth(280)
        self.source.currentIndexChanged.connect(self.source_changed)
        options.addWidget(self.source)
        options.addStretch(1)
        self.fit_button = IconButton("updown", "恢复视图")
        self.fit_button.clicked.connect(self.fit_plot)
        options.addWidget(self.fit_button)
        self.save_button = IconButton("download", "保存当前帧")
        self.save_button.clicked.connect(self.save)
        self.buttons["save"] = self.save_button
        options.addWidget(self.save_button)
        self.points = QSpinBox()
        self.points.setRange(1, 4_000_000)
        self.points.setValue(2000)
        points_row.addWidget(QLabel("请求点数"))
        self.points.setToolTip(
            "请求点数不一定限制仪器传回长度；实际点数见曲线下方。完整波形型号保留全部有效点。"
        )
        points_row.addWidget(self.points)
        settings.addLayout(points_row)
        main_layout.addLayout(options)
        self.actual = QLabel("实际 — s/帧")
        self.numeric_ranges = numeric_ranges = QLabel("点数 1 ～ 4000000（整数） · 新帧到达后尽快显示")
        numeric_ranges.setObjectName("rangeHint")
        numeric_ranges.setWordWrap(True)
        settings.addWidget(numeric_ranges)
        self.channel_row = QHBoxLayout()
        self.channel_checks: dict[int, QCheckBox] = {}
        settings.addLayout(self.channel_row)
        self.scan_channels_label = QLabel()
        self.scan_channels_label.setWordWrap(True)
        self.scan_channels_label.setObjectName("rangeHint")
        settings.addWidget(self.scan_channels_label)
        self.initiate = QCheckBox("读取前执行单次采集")
        self.initiate.setToolTip(
            "点击单次读取或开始监测后按所选方式执行。Pico 必须启动采集；"
            "3458A 读取存储可能停止内部记录。其他设备默认读取现有数据。"
        )
        settings.addWidget(self.initiate)
        self.plot = LivePlot()
        self.plot.plot.frame_save_provider = lambda: page_frame.displayed_frame_saver(
            self, self.selector.device_id, self.controller.mode
        )
        self.plot.plot.setBackground(tk.CHART_BG)
        self.plot.plot.getPlotItem().legend.hide()
        self.plot.value.hide()
        self.plot.meta.hide()
        main_layout.addWidget(self.plot, 1)
        footer = QHBoxLayout()
        self.status = QLabel("仅查看时不保存实验数据")
        self.status.setWordWrap(True)
        footer.addWidget(self.status, 1)
        footer.addWidget(self.actual)
        main_layout.addLayout(footer)
        self.status_dot = page_frame.chart_card(main, status=self.status, footer_extra=(self.actual,))
        splitter.addWidget(main)
        self.form = ControlForm(controller)
        parameters = QTabWidget()
        parameters.addTab(self.form, "仪器参数")
        parameters.addTab(acquisition, "采集设置")
        parameters.setMinimumWidth(340)
        parameters.setMaximumWidth(420)
        self.form.setMinimumHeight(150)
        splitter.addWidget(parameters)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        splitter.setSizes([830, 352])
        layout.addWidget(splitter, 1)
        self.scan_page = None
        if category == "multimeter":
            self.scan_page = ScanMonitoringPage(controller, add_card, self, manage_selection=False)
            layout.addWidget(self.scan_page, 1)
            self.scan_page.hide()
            self.scan_page.selector.selected.connect(lambda device: self.selector.select(device))
        self.selector.selected.connect(self.select_device)
        self.selector.channel_changed.connect(self._select_parameter_channel)
        controller.refreshed.connect(self.refresh)
        controller.frame_ready.connect(self.refresh_frame)
        self.select_device(self.selector.device_id)

    def _select_parameter_channel(self, _channel: int) -> None:
        if self.instrument_scan_channels() is None:
            self.form.set_device(self.selector.device_id, self.selector.channel.currentData() or 1)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        orientation = Qt.Orientation.Vertical if self.width() < 680 else Qt.Orientation.Horizontal
        if self.splitter.orientation() != orientation:
            self.splitter.setOrientation(orientation)
            self.splitter.setSizes([500, 260] if orientation == Qt.Orientation.Vertical else [820, 400])
        QTimer.singleShot(0, self, self.reflow_buttons)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self.refresh_frame(self.selector.device_id)

    def reflow_buttons(self) -> None:
        header_buttons = [self.buttons["once"], self.run_group, self.buttons["stop"], self.buttons["record"]]
        columns = 2 if self.width() < 760 else 4
        if columns == self.button_columns:
            return
        self.button_columns = columns
        while self.button_layout.count():
            self.button_layout.takeAt(0)
        for index, button in enumerate(header_buttons):
            self.button_layout.addWidget(button, index // columns, index % columns)

    def fit_plot(self) -> None:
        self.plot.fit_current()

    def select_device(self, device_id: str) -> None:
        if self.scan_page is not None:
            scan = bool(device_id) and instrument_scan_channels(self.controller, device_id) is not None
            self.scan_page.setVisible(scan)
            self.action_panel.setVisible(not scan and bool(device_id))
            self.header.setVisible(not scan)
            self.selector.setVisible(not scan)
            if scan:
                self.empty.hide()
                self.splitter.hide()
                self.form.set_device("", 1)
                self.scan_page.selector.select(device_id)
                self.scan_page.select_device(device_id)
                return
            if self.scan_page.device_id:
                self.scan_page.select_device("")
        self.timing.bind(device_id, self.controller.mode)
        self.splitter.setVisible(bool(device_id))
        self.action_panel.setVisible(bool(device_id))
        self.empty.setVisible(not device_id)
        self.source.blockSignals(True)
        self.source.clear()
        self.sources = (
            data_sources(config_from_record(self.controller.records[device_id])) if device_id else ()
        )
        for source in self.sources:
            self.source.addItem(source.label, source.key)
        self.source.blockSignals(False)
        while self.channel_row.count():
            item = self.channel_row.takeAt(0)
            widget = item.widget() if item else None
            if widget:
                widget.deleteLater()
        self.channel_checks = {}
        if device_id:
            definition = DEVICE_DEFINITIONS.get(self.controller.records[device_id]["adapterId"])
            channels = (
                self.controller.capabilities(device_id).channels
                if self.category in {"power_supply", "oscilloscope"}
                or (definition and definition.multi_channel_sources)
                else ()
            )
            for index, channel in enumerate(() if self.instrument_scan_channels() is not None else channels):
                check = QCheckBox(f"CH {channel}")
                check.setChecked(index == 0)
                self.channel_checks[channel] = check
                self.channel_row.addWidget(check)
            self.channel_row.addStretch()
            self.initiate.setChecked(bool(definition and definition.acquisition_initiate_default))
            self.initiate.setEnabled(definition is None or definition.acquisition_initiate_editable)
        self.plot.reset()
        self.source_changed()
        self.form.set_device(device_id, self.selector.channel.currentData() or 1)
        self.refresh()
        self.refresh_frame(device_id)

    def channels(self) -> tuple[int, ...]:
        scan = self.instrument_scan_channels()
        if scan is not None:
            return scan
        if not self.channel_checks:
            channel = self.selector.channel.currentData()
            return (channel,) if channel is not None else ()
        return tuple(channel for channel, check in self.channel_checks.items() if check.isChecked())

    def instrument_scan_channels(self) -> tuple[int, ...] | None:
        return instrument_scan_channels(self.controller, self.selector.device_id)

    def source_changed(self) -> None:
        self.plot.reset()
        source = self.selected_source()
        if source:
            self.points.setRange(1, 4_000_000)
            self.points.setValue(source.request((1,)).points)
            self.points.setEnabled(source.kind != "status")
            self.numeric_ranges.setText("请求点数 1 ～ 4000000（整数）")
            device_id = self.selector.device_id
            caps = self.controller.capabilities(device_id) if device_id else None
            point_cap = (
                next((cap for cap in caps.operations if cap.name == "sweep_points"), None) if caps else None
            )
            point = point_cap.parameters.get("value") if point_cap else None
            if (
                source.kind == "spectrum"
                and point_cap
                and not point_cap.writable
                and point
                and point.minimum is not None
                and point.minimum == point.maximum
            ):
                fixed = int(point.minimum)
                self.points.setRange(fixed, fixed)
                self.points.setValue(fixed)
                self.points.setEnabled(False)
                self.numeric_ranges.setText(f"点数 {fixed}（型号固定）")

    def focus_card(self, spec: dict) -> None:
        channels = tuple(spec["channels"])
        self.selector.select(spec["device_id"], channels[0] if channels else 1)
        if self.scan_page is not None and self.instrument_scan_channels() is not None:
            self.scan_page.focus_card(spec)
            return
        self.source.setCurrentIndex(max(0, self.source.findData(spec["source"])))
        for channel, check in self.channel_checks.items():
            check.setChecked(channel in channels)

    def selected_source(self):
        return next((source for source in self.sources if source.key == self.source.currentData()), None)

    def start(self, once: bool = False) -> None:
        if not once and self.controller.reconnect_if_needed(self.selector.device_id):
            return
        device_id, source = self.selector.device_id, self.selected_source()
        if not device_id or not source:
            return
        try:
            self.controller.ensure_applicable(device_id)
            adapter = self.controller.records[device_id]["adapterId"]
            definition = DEVICE_DEFINITIONS.get(adapter)
            # The explicit read/start click is the authorization for this task.
            confirmed = self.initiate.isChecked() or bool(
                definition and definition.acquisition_requires_confirmation
            )
            request = source.request(
                self.channels(), self.points.value(), self.initiate.isChecked(), confirmed
            )
            self.timing.interpretText()
            self.controller.start_monitoring(
                device_id,
                (request,),
                **self.timing.options(),
                mode=self.controller.mode,
                once=bool(once),
            )
        except Exception as exc:
            self.controller.message.emit(str(exc))

    def stop(self) -> None:
        self.controller.watch(self.controller.runtime.monitoring.stop(self.selector.device_id))

    def record(self) -> None:
        device_id = self.selector.device_id
        try:
            self.controller.ensure_applicable(device_id)
            current = self.controller.runtime.monitoring.snapshots().get(device_id, {})
            self.timing.interpretText()
            self.controller.set_recording(
                device_id, not current.get("recording", False), mode=self.controller.mode
            )
        except Exception as exc:
            self.controller.message.emit(str(exc))

    def save(self) -> None:
        frame = self.plot.displayed_frame
        if frame is not None:
            self.controller.save_displayed_frame(self, self.selector.device_id, frame, self.controller.mode)
            self.refresh()

    def refresh(self) -> None:
        scan = self.instrument_scan_channels()
        if self.scan_page is not None and scan is not None:
            self.scan_page.refresh()
            return
        self.scan_channels_label.setVisible(scan is not None)
        self.scan_channels_label.setText(
            "仪器扫描通道："
            + ("、".join(f"CH {ch}" for ch in scan) if scan else "未配置")
            + " · 在参数区配置扫描列表；采集会覆盖仪器内部读数，软件记录另存。"
            if scan is not None
            else ""
        )
        device_id = self.selector.device_id
        state = self.controller.states.get(device_id, {})
        task = self.controller.runtime.monitoring.snapshots().get(device_id, {})
        self.timing.bind(device_id, self.controller.mode)
        self.timing.refresh(task)
        self.actual.setText(actual_interval_text(task))
        update_recording_frame(self.main_area, task)
        record = self.controller.records.get(device_id)
        available = bool(
            record
            and mode_applicable(record, self.controller.mode)
            and state.get("connectionState") in {"connected", "busy"}
            and state.get("activeTask") != "continuous_acquisition"
            and not task.get("stopping")
            and recording_phase(task) not in {"preparing", "draining"}
        )
        self.buttons["start"].setText("开始监测")
        self.buttons["start"].setEnabled(available)
        refresh_reconnect_button(self.buttons["start"], self.controller, device_id, task)
        self.buttons["once"].setEnabled(available and not task.get("running") and not task.get("pending"))
        self.buttons["stop"].setEnabled(bool(task.get("running") or task.get("pending")))
        self.buttons["record"].setEnabled(bool((available or task.get("recording")) and task.get("running")))
        self.buttons["record"].setText("停止记录" if task.get("recording") else "开始记录")
        style_button(
            self.buttons["record"],
            "danger" if task.get("recording") else "",
            "stop" if task.get("recording") else "record",
        )
        self.buttons["save"].setEnabled(
            self.plot.displayed_frame is not None
            and not task.get("display_saving")
            and not task.get("capture_saving")
        )
        self.fit_button.setEnabled(self.plot.displayed_frame is not None)
        text = (
            task.get("error")
            or task.get("recording_notice")
            or task.get("notice")
            or recording_save_notice(task)
            or (
                "正在记录原始数据"
                if task.get("recording")
                else "监测中 · 尚未开启记录"
                if task.get("running")
                else "已停止软件读取 · 仪器输出保持原状"
            )
        )
        page_frame.show_state(self.status_dot, task, available)
        if text != self.status.text():
            self.status.setText(text)
        if self.isVisible():
            self.form.set_device(device_id, self.selector.channel.currentData() or 1)

    def refresh_frame(self, device_id: str) -> None:
        if getattr(self, "scan_page", None) is not None and self.instrument_scan_channels() is not None:
            return
        source = self.selected_source()
        if device_id != self.selector.device_id or not self.isVisible() or not source:
            return
        service = self.controller.runtime.monitoring
        frame = service.latest(
            device_id, source.kind, source.quantity, self.channels()[0] if self.channels() else 1
        )
        if frame:
            self.plot.show_frame(frame, source, self.channels())
            service.displayed(device_id, frame)
