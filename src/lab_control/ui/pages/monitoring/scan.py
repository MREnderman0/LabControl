"""Monitor an instrument-owned scan list without editing input parameters."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from lab_control.application.acquisition_pipeline import validate_displayed_batch
from lab_control.domain.instruments import DeviceError
from lab_control.ui.pages.monitoring.channel_selection import instrument_scan_channels
from lab_control.ui.pages.monitoring.optical_power import (
    OpticalPowerMonitoringPage,
    OpticalPowerPlot,
    PowerMeterReadoutCard,
    _scalar,
)
from lab_control.ui.pages.monitoring.readout_columns import add_readout_column


class ScanTrendPlot(OpticalPowerPlot):
    quantity = ""
    display_unit = ""
    axis_label = "测量值"
    nonnegative = False

    def __init__(self, parent=None):
        super().__init__(parent)
        self.measurement_kind = None

    def display_value(self, value: float, unit: str) -> float | None:
        return value

    def set_channels(self, channels: tuple[int, ...]) -> None:
        # Every readout card still displays its measurement when its curve is
        # hidden. Preserve that complete frame for the explicit snapshot save.
        self.channels = tuple(channels)
        for channel, curve in self.curves.items():
            curve.setVisible(channel in self.channels)

    def show_frame(self, frame, channels: tuple[int, ...]) -> bool:
        visible = self.channels
        wanted = tuple(m for m in frame.batch.measurements if m.channel in channels)
        kinds = {(m.quantity, m.unit) for m in wanted}
        if (
            not channels
            or len(wanted) != len(channels)
            or {m.channel for m in wanted} != set(channels)
            or len(kinds) != 1
            or any(_scalar(m.values) is None for m in wanted)
        ):
            return False
        try:
            validate_displayed_batch(frame.batch)
        except DeviceError:
            return False
        kind = next(iter(kinds))
        if self.measurement_kind != kind:
            self.reset()
            self.measurement_kind = kind
            quantity, unit = kind
            self.axis_label = {"temperature": "温度", "resistance": "电阻", "voltage": "电压"}.get(
                quantity, "测量值"
            )
            self.display_unit = unit
            self.plot.setLabel("left", self.axis_label, units=unit)
        accepted = super().show_frame(frame, channels)
        self.set_channels(visible)
        if accepted:
            self.fit_current()
        return accepted


class ScanReadoutCard(PowerMeterReadoutCard):
    """A compact list cell; two consecutive channels share each visual row."""

    display_unit = ""
    WIDTH = 260
    HEIGHT = 28

    def __init__(self, channel: int, parent=None) -> None:
        super().__init__(channel, parent)
        self.setObjectName("scanReadoutCell")
        self.setMinimumWidth(0)
        self.setMaximumWidth(16777215)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        layout = self.layout()
        assert layout is not None
        layout.setContentsMargins(4, 0, 4, 0)
        layout.setSpacing(0)
        self.status.hide()
        self.meta.hide()
        layout.removeWidget(self.value)
        self.heading.takeAt(1)  # The former card header's stretch would squeeze the reading.
        self.heading.setSpacing(6)
        self.channel_label.hide()
        self.check = QCheckBox()
        self.check.setStyleSheet(self.channel_label.styleSheet())
        self.value.setObjectName("scanReadoutValue")
        self.value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.value.setText("—")
        self.unit = QLabel(self.display_unit)
        self.unit.setObjectName("scanReadoutUnit")
        self.unit.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.peak = QLabel("—")
        self.peak.setObjectName("scanReadoutValue")
        self.peak.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        scan_columns(self.heading, (self.check, self.value, self.unit, self.peak))

    def display_value(self, value: float, unit: str) -> float | None:
        return value

    def update_measurement(self, measurement, timestamp=None) -> None:
        if measurement:
            self.display_unit = measurement.unit
        super().update_measurement(measurement, timestamp)
        self.value.setText(self.value.text().removesuffix(f" {self.display_unit}"))
        self.unit.setText(self.display_unit)
        self.value.setAccessibleName(f"CH {self.channel} · {self.value.text()} {self.display_unit}")
        if measurement and measurement.quantity == "temperature":
            self.meta.setText("通道温度")
        elif measurement:
            self.meta.setText(
                {"resistance": "两线电阻", "voltage": "直流电压"}.get(measurement.quantity, "仪器读数")
            )
        if measurement:
            self.setToolTip(f"{self.toolTip()}\n{self.meta.text()}\n更新时间：{timestamp or '未提供'}")
            self.value.setToolTip(self.toolTip())
        self.unit.setToolTip(self.value.toolTip())

    def update_temperature_window(self, snapshot) -> None:
        value = snapshot.peak_to_peak if snapshot else None
        self.peak.setText("—" if value is None else f"{value:.6g}")
        self.peak.setAccessibleName(f"CH {self.channel} 温度峰峰值 {self.peak.text()}")
        self.peak.setToolTip(
            "不足 30 min 使用本轮全部有效样本；超过后仅使用最近 30 min（含边界）。\n"
            + (
                f"最小值：{snapshot.minimum:.9g} {snapshot.unit}\n"
                f"最大值：{snapshot.maximum:.9g} {snapshot.unit}\n"
                f"峰峰值：{value:.9g} {snapshot.unit} · 样本数：{snapshot.count}"
                if value is not None
                else "窗口内无有效温度样本"
            )
        )


def scan_columns(layout: QHBoxLayout, fields: tuple[QWidget, ...]) -> None:
    layout.setSpacing(6)
    for field, (width, stretch) in zip(fields, ((65, 2), (53, 1), (27, 0), (78, 0)), strict=True):
        add_readout_column(layout, field, width, stretch)


def scan_readout_header(parent=None) -> QWidget:
    header = QWidget(parent)
    grid = QGridLayout(header)
    grid.setContentsMargins(0, 0, 0, 0)
    grid.setHorizontalSpacing(12)
    for column in range(2):
        group = QWidget(header)
        row = QHBoxLayout(group)
        row.setContentsMargins(4, 0, 4, 0)
        channel, value, unit = QLabel("曲线 · 通道"), QLabel("读数"), QLabel("单位")
        channel.setObjectName("powerReadoutHeading")
        value.setObjectName("powerReadoutHeading")
        unit.setObjectName("powerReadoutHeading")
        value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        peak = QLabel("峰峰值 · 30 min")
        peak.setObjectName("powerReadoutHeading")
        peak.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        scan_columns(row, (channel, value, unit, peak))
        grid.addWidget(group, 0, column)
        grid.setColumnStretch(column, 1)
    header.setFixedHeight(26)
    return header


class ScanMonitoringPage(OpticalPowerMonitoringPage):
    """A trend and a two-column readout list, recording every scanned input."""

    category = "multimeter"
    rolling_power = False
    vertical_below = 680
    category_name = "测量设备"
    trend_title = "测量趋势"
    quantity = ""
    plot_type = ScanTrendPlot
    card_type = ScanReadoutCard

    def __init__(self, controller, add_card, parent=None, *, manage_selection=True):
        super().__init__(controller, add_card, parent, manage_selection=manage_selection)
        self.notice = QLabel("温度峰峰值使用最近 30 min 的有效温度。")
        self.notice.setObjectName("mutedText")
        self.notice.setWordWrap(True)
        self.main_layout.insertWidget(2, self.notice)
        self.save_button = QPushButton("保存当前帧")
        self.save_button.clicked.connect(self.save)
        self.chart_actions.insertWidget(2, self.save_button)
        self.channel_bar.hide()
        readout_panel = self.splitter.widget(1)
        assert readout_panel is not None
        readout_panel.setObjectName("scanReadoutList")
        readout_panel.setMinimumWidth(280)
        panel_layout = readout_panel.layout()
        assert isinstance(panel_layout, QVBoxLayout)
        panel_layout.setContentsMargins(10, 8, 10, 8)
        self.scan_summary = QLabel(self.channel_title.text(), readout_panel)
        self.scan_summary.setObjectName("scanReadoutSummary")
        self.scan_summary.setWordWrap(True)
        panel_layout.insertWidget(1, self.scan_summary)
        self.readout_heading = scan_readout_header(readout_panel)
        panel_layout.insertWidget(2, self.readout_heading)
        self.readout_grid.setHorizontalSpacing(12)
        self.readout_grid.setVerticalSpacing(0)
        self.splitter.setStretchFactor(1, 0)
        self.splitter.setSizes([620, 560])
        self._reflow_readout_cards()
        self.refresh()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        compact = self.vertical_below <= self.width() < 960
        if compact != getattr(self, "_compact_split", None):
            self._compact_split = compact
            if self.splitter.orientation() == Qt.Orientation.Horizontal:
                width = self.splitter.width()
                right = min(560, max(280, int(width * 0.43))) if compact else 560
                self.splitter.setSizes([max(1, width - right), right])

    def _reflow_readout_cards(self) -> None:
        width = self.readout_scroll.viewport().width()
        columns = 2 if width >= 520 else 1
        # Two compact groups share the viewport. Give a named sensor the full row
        # when its checkbox and the aligned numeric columns cannot fit side by side.
        named = self.plot.names
        for channel, card in self.cards.items():
            if str(channel) in named and isinstance(card, ScanReadoutCard):
                cell_width = (width - self.readout_grid.horizontalSpacing()) // 2
                available = (
                    cell_width - 8 - 3 * card.heading.spacing() - card.unit.width() - card.peak.width()
                )
                if card.check.sizeHint().width() > min(
                    available * 2 // 3, available - card.value.minimumWidth()
                ):
                    columns = 1
                    break
        order = self.cards if named else sorted(self.cards)
        for index, channel in enumerate(order):
            card = self.cards[channel]
            self.readout_grid.addWidget(card, index // columns, index % columns)
            card.show()
        self.readout_grid.setColumnStretch(0, 1)
        self.readout_grid.setColumnStretch(1, int(columns == 2))
        if self.readout_heading is not None:
            grid = self.readout_heading.layout()
            assert isinstance(grid, QGridLayout)
            second_group = grid.itemAtPosition(0, 1)
            assert second_group is not None
            heading = second_group.widget()
            assert heading is not None
            heading.setVisible(columns == 2)
            grid.setColumnStretch(1, int(columns == 2))
            self.readout_heading.setContentsMargins(
                0, 0, max(0, self.readout_scroll.width() - self.readout_scroll.viewport().width()), 0
            )

    def refresh_channel_names(self) -> None:
        previous = self._names_key
        super().refresh_channel_names()
        if self._names_key != previous:
            self._reflow_readout_cards()

    def available_channels(self, device_id: str) -> tuple[int, ...]:
        return instrument_scan_channels(self.controller, device_id) or ()

    def channels(self) -> tuple[int, ...]:
        return self.available_channels(self.device_id) if self.device_id else ()

    def displayed_channels(self) -> tuple[int, ...]:
        return tuple(ch for ch, check in self.channel_checks.items() if check.isChecked())

    def select_device(self, device_id: str) -> None:
        super().select_device(device_id)
        self.channel_title.setText(
            f"仪器扫描 {len(self.channels())} 路 · 记录保留全部通道 · 勾选显示曲线"
            if self.channels()
            else "仪器扫描列表为空，请在仪器面板配置后重新连接。"
        )
        if hasattr(self, "scan_summary"):
            self.scan_summary.setText(self.channel_title.text())
        self.plot.set_channels(self.displayed_channels())

    def channels_changed(self) -> None:
        self.channel_preferences[self._channel_key(self.device_id)] = list(self.displayed_channels())
        self.controller.runtime.repository.save_workspace(
            f"monitoring_channels/{self.category}", self.channel_preferences
        )
        self.plot.set_channels(self.displayed_channels())
        self.refresh()

    def refresh_frame(self, device_id: str) -> None:
        super().refresh_frame(device_id)
        if device_id == self.device_id:
            assert isinstance(self.plot, ScanTrendPlot)
            snapshots = self.controller.runtime.monitoring.temperature_window(device_id)
            histories = (
                {s.channel: s for s in snapshots}
                if self.plot.measurement_kind and self.plot.measurement_kind[0] == "temperature"
                else {}
            )
            for channel, card in self.cards.items():
                if isinstance(card, ScanReadoutCard):
                    card.update_temperature_window(histories.get(channel))
        self.plot.set_channels(self.displayed_channels())
        self.trend_label.setText(
            f"{self.plot.axis_label}趋势（{self.plot.display_unit}）"
            if self.plot.display_unit
            else self.trend_title
        )

    def refresh(self) -> None:
        if self.device_id and tuple(self.channel_checks) != self.channels():
            self.select_device(self.device_id)
            return
        super().refresh()
        if hasattr(self, "save_button"):
            task = self.controller.runtime.monitoring.snapshots().get(self.device_id, {})
            self.save_button.setEnabled(
                self.plot.displayed_frame is not None
                and not task.get("display_saving")
                and not task.get("capture_saving")
            )

    def save(self) -> None:
        frame = self.plot.displayed_frame
        if frame is None:
            return
        try:
            future = self.controller.runtime.monitoring.export_displayed_frame(
                self.device_id, frame, self.mode
            )
            self.controller.watch(
                future, lambda path: self.controller.message.emit(f"原始数据已保存 · 分析 CSV 已完成：{path}")
            )
            self.refresh()
        except Exception as exc:
            self.controller.message.emit(str(exc))

    def focus_card(self, spec) -> None:
        self.selector.select(spec["device_id"])
        # The pinned card is a view; never replace the instrument-owned scan list.
