"""Offline plots with a latest-request-only frame loader and full-record scalar views."""

from __future__ import annotations

from concurrent.futures import Future
from html import escape
from typing import Any

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QSlider,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from lab_control.application.processing import ProcessingService
from lab_control.domain.oscilloscope import ScopeDisplay, engineering
from lab_control.domain.workspace import channel_label
from lab_control.storage.processing_results import ResultReader, plot_series
from lab_control.ui.styles import tokens as tk
from lab_control.ui.widgets.plot_export import PublicationPlotWidget
from lab_control.ui.widgets.workspace_combo import WorkspaceComboBox

CHANNEL_PALETTE = tk.CHANNEL_COLORS
CHANNEL_STYLES = (Qt.PenStyle.SolidLine, Qt.PenStyle.DashLine, Qt.PenStyle.DotLine)


class ProcessingViewer(QWidget):
    def __init__(self, service: ProcessingService, parent=None):
        super().__init__(parent)
        self.service = service
        self.reader: ResultReader | None = None
        self.info: dict[str, Any] = {}
        self.transfer_index = False
        self.future: Future | None = None
        self.pending: tuple | None = None
        self.token = 0
        self.loading_token = 0
        self.loading_kind = ""
        self._fit_pending = True
        self._drawing = False
        self.scope_specs: dict[str, ScopeDisplay] = {}
        self._scope_signature: tuple = ()
        self.plot_data: list[tuple[str, np.ndarray, np.ndarray]] = []
        self.cursor_data: list[tuple[str, np.ndarray, np.ndarray]] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        tools = QHBoxLayout()
        self.group = WorkspaceComboBox()
        self.group.setMinimumWidth(150)
        self.group.currentIndexChanged.connect(self._group_changed)
        tools.addWidget(self.group)
        self.watts = QCheckBox("以 W 显示")
        self.watts.toggled.connect(lambda: self.request(fit=True))
        tools.addWidget(self.watts)
        self.scope_view = QCheckBox("示波器刻度")
        self.scope_view.setToolTip("按采集时各通道的量程和偏移显示；取消勾选查看统一物理单位坐标")
        self.scope_view.setChecked(True)
        self.scope_view.hide()
        self.scope_view.toggled.connect(lambda: self.request(fit=True))
        tools.addWidget(self.scope_view)
        fit = QPushButton("适应全部数据")
        fit.clicked.connect(self.fit_all)
        tools.addWidget(fit)
        tools.addStretch()
        layout.addLayout(tools)
        self.temperature_note = QLabel("温度峰峰值：记录末尾 30 min；不足 30 min 使用全部有效数据。")
        self.temperature_note.setObjectName("mutedText")
        self.temperature_note.setWordWrap(True)
        self.temperature_note.hide()
        layout.addWidget(self.temperature_note)
        split = QSplitter()
        self.channels = QTreeWidget()
        self.channels.setRootIsDecorated(False)
        self.channels.setHeaderLabels(["通道", "峰峰值"])
        self.channels.header().setStretchLastSection(False)
        self.channels.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.channels.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.channels.setColumnHidden(1, True)
        self.channels.setHeaderHidden(True)
        self.channels.itemChanged.connect(lambda _item, _column: self.request(fit=True))
        split.addWidget(self.channels)
        self.plot = PublicationPlotWidget(background=tk.CHART_BG)
        self.plot.disableAutoRange()
        self.plot.showGrid(x=True, y=True, alpha=0.15)
        self.plot.addLegend(
            offset=(8, 8),
            brush=pg.mkBrush(tk.SURFACE),
            pen=pg.mkPen(tk.BORDER_STRONG),
            labelTextColor=tk.TEXT_2,
        )
        self.plot.setMinimumSize(300, 260)
        self.plot.getViewBox().sigXRangeChanged.connect(self._range_changed)
        self.plot.scene().sigMouseMoved.connect(self._cursor)
        split.addWidget(self.plot)
        split.setStretchFactor(1, 1)
        layout.addWidget(split, 1)
        self.timeline_row = QWidget()
        timeline = QHBoxLayout(self.timeline_row)
        timeline.setContentsMargins(0, 0, 0, 0)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.previous = QPushButton("上一帧")
        self.previous.clicked.connect(lambda: self.slider.setValue(self.slider.value() - 1))
        timeline.addWidget(self.previous)
        self.slider.setAccessibleName("采集帧时间线")
        self.slider.valueChanged.connect(lambda: self.request())
        timeline.addWidget(self.slider, 1)
        self.next = QPushButton("下一帧")
        self.next.clicked.connect(lambda: self.slider.setValue(self.slider.value() + 1))
        timeline.addWidget(self.next)
        layout.addWidget(self.timeline_row)
        self.status = QLabel("选择已处理的设备")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.cursor_text = QLabel("移动光标查看曲线读数；拖动图表平移，滚轮缩放。")
        self.cursor_text.setTextFormat(Qt.TextFormat.PlainText)
        self.cursor_text.setWordWrap(True)
        self.cursor_text.setObjectName("mutedText")
        layout.addWidget(self.cursor_text)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._poll)
        self.timer.start(40)
        self.range_timer = QTimer(self)
        self.range_timer.setSingleShot(True)
        self.range_timer.timeout.connect(lambda: self.request())

    def clear(self, text="设备尚未处理") -> None:
        self.range_timer.stop()
        self.token += 1
        self.pending = None
        self.reader = None
        self.info = {}
        self.plot.clear()
        self.plot_data.clear()
        self.cursor_data.clear()
        self.scope_specs.clear()
        self._scope_signature = ()
        self.scope_view.hide()
        self.channels.clear()
        self.temperature_note.hide()
        self.group.clear()
        self.timeline_row.hide()
        self.status.setText(text)

    def open_result(self, folder) -> None:
        self.clear("正在加载处理结果…")
        self.reader = ResultReader(folder)
        self.token += 1
        self.pending = (self.token, "info", self.reader, ())
        self._poll()

    def _channel_name(self, series) -> str:
        return channel_label(
            self.info.get("recording", {}).get("channel_names", {}), series.get("channel", 1)
        )

    def _group_changed(self) -> None:
        self.channels.blockSignals(True)
        self.channels.clear()
        group = self.group.currentData()
        self.temperature_note.setVisible(bool(group and group[0] == "temperature"))
        stats = self.info.get("temperature_statistics", {})
        by_series = {row["series_id"]: row for row in stats.get("channels", [])}
        show_peaks = bool(group and group[0] == "temperature" and by_series)
        self.channels.setColumnHidden(1, not show_peaks)
        self.channels.setHeaderHidden(not show_peaks)
        self.channels.setMaximumWidth(340 if show_peaks else 220)
        self.watts.blockSignals(True)
        self.watts.setChecked(bool(group and group[1] == "W"))
        self.watts.blockSignals(False)
        for series in self.info.get("series", []):
            if (series["quantity"], series.get("unit", ""), series.get("axis_unit", "")) != group:
                continue
            name = self._channel_name(series)
            item = QTreeWidgetItem([name])
            item.setToolTip(0, name)
            if series["id"] in by_series:
                row = by_series[series["id"]]
                peak = row["peak_to_peak"]
                item.setText(1, "—" if peak is None else f"{peak:.6g} {row['unit']}")
                item.setTextAlignment(1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                item.setToolTip(
                    1,
                    f"统计区间：{stats['start_elapsed']:g}–{stats['end_elapsed']:g} s（记录末尾 30 min）\n"
                    f"样本数：{row['count']}\n最小值：{row['minimum']} {row['unit']}\n"
                    f"最大值：{row['maximum']} {row['unit']}\n峰峰值：{peak} {row['unit']}",
                )
            item.setData(0, Qt.ItemDataRole.UserRole, series)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(0, Qt.CheckState.Checked)
            self.channels.addTopLevelItem(item)
        self.channels.blockSignals(False)
        self.request(fit=True)

    def selected(self) -> list[dict]:
        items = (self.channels.topLevelItem(i) for i in range(self.channels.topLevelItemCount()))
        return [
            item.data(0, Qt.ItemDataRole.UserRole)
            for item in items
            if item is not None and item.checkState(0) == Qt.CheckState.Checked
        ]

    def fit_all(self) -> None:
        self.range_timer.stop()
        self.request(fit=True)

    def request(self, *, fit=False) -> None:
        if self.reader is None or not self.info:
            return
        self._fit_pending |= fit
        self.token += 1
        if self.info["kind"] == "scalar":
            bounds = None if self._fit_pending else tuple(self.plot.viewRange()[0])
            args = (tuple(s["id"] for s in self.selected()), bounds)
            self.pending = (self.token, "scalar", self.reader, args)
        else:
            self.pending = (self.token, "frame", self.reader, (self.slider.value(),))
        self.previous.setEnabled(self.slider.value() > self.slider.minimum())
        self.next.setEnabled(self.slider.value() < self.slider.maximum())
        self._poll()

    def _poll(self) -> None:
        if self.future is not None and self.future.done():
            future, self.future = self.future, None
            try:
                data = future.result()
                if self.loading_token == self.token:
                    self._loaded(self.loading_kind, data)
            except Exception as exc:
                if self.loading_token == self.token:
                    self.status.setText(f"读取失败：{exc}")
        if self.future is None and self.pending is not None and not self.service.closed:
            token, kind, reader, args = self.pending
            self.pending = None
            self.loading_token, self.loading_kind = token, kind
            self.future = self.service.read_worker.submit(getattr(reader, kind), *args)

    def _loaded(self, kind, data) -> None:
        if kind == "info":
            data = {**data, "series": list(plot_series(data["series"]))}
            self.info = data
            self._fit_pending = True
            self.watts.setVisible(bool(data.get("power_units")))
            self.watts.setChecked(False)
            groups = list(
                dict.fromkeys(
                    (s["quantity"], s.get("unit", ""), s.get("axis_unit", "")) for s in data["series"]
                )
            )
            self.group.blockSignals(True)
            self.group.clear()
            labels = {
                "power": "功率",
                "frequency": "频率",
                "temperature": "温度",
                "waveform": "波形",
                "spectrum": "频谱",
                "voltage": "电压",
                "resistance": "电阻",
                "selected_s_parameter_magnitude": "S 参数幅值",
                "selected_s_parameter_phase": "S 参数相位",
            }
            for group in groups:
                label = (
                    "光谱" if group[0] == "spectrum" and group[2] == "nm" else labels.get(group[0], group[0])
                )
                self.group.addItem(f"{label} · {group[1]}", group)
            self.group.blockSignals(False)
            self.slider.blockSignals(True)
            self.slider.setRange(1, max(1, data["frames"]))
            self.slider.setValue(1)
            self.slider.blockSignals(False)
            self.slider.setEnabled(data["frames"] > 1)
            self.timeline_row.setVisible(data["kind"] == "frames")
            if not data["frames"]:
                self.status.setText("该记录没有有效采集帧")
            else:
                self._group_changed()
            return
        points = []
        channel_numbers = []
        self.transfer_index = False
        self.scope_specs = {}
        selected = self.selected()
        samples = list(plot_series(data["measurements"])) if kind == "frame" else ()
        for series in selected:
            if kind == "scalar":
                x, y = data.get(series["id"], (np.array([]), np.array([])))
            else:
                sample = next(
                    (
                        sample
                        for sample in samples
                        if all(
                            sample.get(key, "") == series.get(key, "")
                            for key in ("channel", "quantity", "unit", "axis_unit")
                        )
                    ),
                    None,
                )
                if sample is None:
                    continue
                y = sample["values"].reshape(-1)
                x = np.arange(y.size) if sample["axis"] is None else sample["axis"].reshape(-1)
                metadata = sample.get("metadata", {})
                self.transfer_index |= metadata.get("axis_kind") == "transfer_sample_index"
                display = metadata.get("scope_display")
                if display and series.get("axis_unit") == "s":
                    spec = ScopeDisplay.parse(display)
                    if spec.unit != series["unit"]:
                        raise ValueError("波形单位与采集时示波器刻度不一致")
                    self.scope_specs[f"CH {series.get('channel', 1)}"] = spec
            power = bool(self.info.get("power_units"))
            if power and self.watts.isChecked() and series.get("unit") == "dBm":
                y = np.power(10.0, np.asarray(y) / 10.0) * 1e-3
            elif power and not self.watts.isChecked() and series.get("unit") == "W":
                with np.errstate(divide="ignore", invalid="ignore"):
                    y = np.where(np.asarray(y) > 0, 10 * np.log10(np.asarray(y) * 1e3), np.nan)
            points.append((self._channel_name(series), x, y))
            channel_numbers.append(series.get("channel", 1))
        available = bool(points) and len(self.scope_specs) == len(points)
        self.scope_view.setVisible(available)
        if not available:
            self.scope_specs.clear()
        signature = tuple(self.scope_specs.items()) if self.scope_view.isChecked() else ()
        if signature != self._scope_signature:
            self._fit_pending = True
            self._scope_signature = signature
        self._draw(points, channel_numbers)
        if kind == "frame":
            self.status.setText(
                f"帧 {data['sequence']} / {self.info['frames']} · {data['timestamp']} · "
                f"距开始 {data['elapsed']:.6g} s；时间沿用原始读回记录。"
            )
        else:
            warning = self.info.get("warning", "")
            self.status.setText(
                f"完整记录 {self.info['frames']:,} 帧 · {len(selected)} 个选中通道 · "
                f"{self.info['range'][1]:.6g} s" + (f" · 原记录提示：{warning}" if warning else "")
            )

    def _draw(self, points, channel_numbers=None) -> None:
        self._drawing = True
        try:
            self.plot.clear()
            self.plot_data = points
            self.cursor_data = []
            displayed = []
            scope = bool(self.scope_specs) and self.scope_view.isChecked()
            for name, x, y in points:
                if scope:
                    spec = self.scope_specs[name]
                    displayed.append((name, x, (y - spec.reference_value) / spec.units_per_div))
                else:
                    displayed.append((name, x, y))
            # Bounds must come from the full loaded arrays, before clipToView or
            # downsampling see the previous viewport (possibly far off screen).
            if self._fit_pending and displayed:
                self._fit_points(displayed)
                self._fit_pending = False
            for i, (name, x, y) in enumerate(points):
                if self.info.get("kind") == "frames" and np.all(x[1:] >= x[:-1]):
                    self.cursor_data.append((name, x, y))
                else:
                    valid = np.isfinite(x) & np.isfinite(y)
                    order = np.argsort(x[valid], kind="stable")
                    self.cursor_data.append((name, x[valid][order], y[valid][order]))
                channel = (channel_numbers[i] if channel_numbers is not None else int(name.split()[-1])) - 1
                curve = self.plot.plot(
                    x,
                    displayed[i][2],
                    pen=pg.mkPen(
                        CHANNEL_PALETTE[channel % len(CHANNEL_PALETTE)],
                        width=1.3,
                        style=CHANNEL_STYLES[(channel // len(CHANNEL_PALETTE)) % len(CHANNEL_STYLES)],
                    ),
                    name=(
                        f"{name} · {engineering(self.scope_specs[name].units_per_div, self.scope_specs[name].unit)}/格"
                        if scope
                        else escape(name)
                    ),
                    connect="finite",
                    symbol="o" if len(x) == 1 else None,
                    useCache=False,
                )
                # Scalar envelopes contain irregular times and NaN gap markers;
                # pyqtgraph's clip/downsample assumes sorted, uniform X values.
                uniform = (
                    self.info.get("kind") == "frames"
                    and len(x) > 1
                    and np.isfinite(x).all()
                    and np.all(np.diff(x) > 0)
                    and np.allclose(np.diff(x), x[1] - x[0], rtol=1e-5, atol=0)
                )
                curve.setClipToView(bool(uniform))
                curve.setDownsampling(auto=bool(uniform), method="peak")
            group = self.group.currentData() or ("", "", "")
            unit = self.display_unit(group[1])
            y_prefix = not scope and unit in {"W", "V", "A", "Hz", "Ω"}
            self.plot.setLabel(
                "left",
                "垂直格（各通道独立刻度）" if scope else self.group.currentText().split(" · ")[0],
                units="" if scope else unit,
                siPrefixEnableRanges=None if y_prefix else (),
            )
            self.plot.getAxis("left").enableAutoSIPrefix(y_prefix)
            axis = "s" if self.info.get("kind") == "scalar" else group[2]
            self.plot.setLabel(
                "bottom",
                "距记录开始"
                if self.info.get("kind") == "scalar"
                else "传输样本序号（频率未核实）"
                if self.transfer_index and axis == "index"
                else {"Hz": "频率", "nm": "波长", "s": "帧内时间", "index": "样本序号"}.get(axis, "横轴"),
                units=axis,
                siPrefixEnableRanges=None if axis == "s" else (),
            )
            self.plot.getAxis("bottom").enableAutoSIPrefix(axis == "s")
        finally:
            self._drawing = False

    def _fit_points(self, points) -> None:
        valid = [
            (x[np.isfinite(x) & np.isfinite(y)], y[np.isfinite(x) & np.isfinite(y)]) for _, x, y in points
        ]
        valid = [(x, y) for x, y in valid if x.size]
        if not valid:
            return
        xlo, xhi = min(float(x.min()) for x, _ in valid), max(float(x.max()) for x, _ in valid)
        ylo, yhi = min(float(y.min()) for _, y in valid), max(float(y.max()) for _, y in valid)

        def padded(lo, hi):
            padding = (hi - lo) * 0.04 if hi > lo else max(abs(lo) * 1e-6, 1e-9)
            return lo - padding, hi + padding

        self.plot.setRange(xRange=padded(xlo, xhi), yRange=padded(ylo, yhi), padding=0, disableAutoRange=True)

    def _range_changed(self, *_args) -> None:
        if not self._drawing and self.info.get("kind") == "scalar":
            self.range_timer.start(120)

    def _cursor(self, position) -> None:
        if not self.plot.sceneBoundingRect().contains(position):
            return
        point = self.plot.getViewBox().mapSceneToView(position)
        values = []
        group = self.group.currentData() or ("", "", "")
        unit = self.display_unit(group[1])
        axis_unit = "s" if self.info.get("kind") == "scalar" else group[2]
        for label, xx, yy in self.cursor_data:
            if xx.size:
                index = min(int(np.searchsorted(xx, point.x())), xx.size - 1)
                if index and abs(xx[index - 1] - point.x()) < abs(xx[index] - point.x()):
                    index -= 1
                values.append(f"{label}: {yy[index]:.9g} {unit}")
        self.cursor_text.setText(f"X = {point.x():.9g} {axis_unit} · " + "；".join(values))

    def display_unit(self, unit: str) -> str:
        if self.info.get("power_units") and unit in {"W", "dBm"}:
            return "W" if self.watts.isChecked() else "dBm"
        return unit
