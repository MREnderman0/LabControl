"""Local spectrum overlays; all calculations use the full, un-decimated trace."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Any

import pyqtgraph as pg
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QMenu,
    QPushButton,
    QVBoxLayout,
)

from lab_control.domain.instruments import Parameter
from lab_control.domain.spectrum import MarkerReading, SpectrumMarker, locate_marker
from lab_control.domain.spectrum_display import display_values
from lab_control.ui.pages.device_control.editors import ParameterEditor
from lab_control.ui.pages.monitoring.plot import LivePlot
from lab_control.ui.pages.monitoring.spectrum_view import SpectrumCanvas, SpectrumView
from lab_control.ui.styles import tokens as tk


def frequency_text(value: float) -> str:
    scale, unit = next(
        ((s, u) for s, u in ((1e9, "GHz"), (1e6, "MHz"), (1e3, "kHz")) if abs(value) >= s), (1, "Hz")
    )
    return f"{value / scale:.8g} {unit}"


class MarkerDialog(QDialog):
    def __init__(self, marker: SpectrumMarker, parent=None, *, creating=False) -> None:
        super().__init__(parent)
        self.marker, self.deleted = marker, False
        self.setWindowTitle("添加标记" if creating else f"编辑 M{marker.number}")
        self.setMinimumWidth(360)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("标记频率 / 寻峰中心", self))
        self.frequency = ParameterEditor(Parameter(unit="Hz", minimum=0), self)
        self.frequency.set_value(marker.frequency)
        self.frequency.range_label.hide()
        layout.addWidget(self.frequency)
        self.tracking = QCheckBox("持续寻峰 · 跟踪范围内最高峰", self)
        self.tracking.setChecked(marker.tracking)
        layout.addWidget(self.tracking)
        layout.addWidget(QLabel("搜索半宽（中心频率 ± 此值）", self))
        self.search_width = ParameterEditor(Parameter(unit="Hz", minimum=1e-12), self)
        self.search_width.set_value(marker.half_width)
        self.search_width.range_label.hide()
        self.search_width.setEnabled(marker.tracking)
        self.tracking.toggled.connect(self.search_width.setEnabled)
        layout.addWidget(self.search_width)
        self.error = QLabel(self)
        self.error.setWordWrap(True)
        layout.addWidget(self.error)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("添加" if creating else "保存")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        if not creating:
            delete = QPushButton("删除标记", self)
            delete.clicked.connect(self.remove)
            buttons.addButton(delete, QDialogButtonBox.ButtonRole.DestructiveRole)
        layout.addWidget(buttons)

    def remove(self) -> None:
        self.deleted = True
        self.accept()

    def save(self) -> None:
        try:
            self.frequency.finish_editing(force=True)
            self.search_width.finish_editing(force=True)
            self.marker = replace(
                self.marker,
                frequency=self.frequency.value(),
                tracking=self.tracking.isChecked(),
                half_width=self.search_width.value(),
            )
            self.accept()
        except ValueError as exc:
            self.error.setText(str(exc))


class MarkerLine(pg.InfiniteLine):
    edit_requested = Signal()

    def mouseClickEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and event.double():
            self.edit_requested.emit()
            event.accept()
        else:
            super().mouseClickEvent(event)


class MarkerLabel(pg.TextItem):
    edit_requested = Signal()

    def mouseClickEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and event.double():
            self.edit_requested.emit()
            event.accept()
        else:
            event.ignore()


class SpectrumPlot(LivePlot):
    trace_view: SpectrumView
    y_axis_label = "幅度"
    canvas_type = SpectrumCanvas
    trace_view_type = SpectrumView
    edit_marker = Signal(int)
    add_frequency = Signal(float)
    save_frame = Signal(object)
    readings_changed = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.markers: list[SpectrumMarker] = []
        self.readings: dict[int, MarkerReading] = {}
        self.overlays: dict[int, tuple[MarkerLine, MarkerLabel, Any]] = {}
        self.trace = None
        self.amplitude_unit = ""
        self.save_enabled = False
        self.plot.menu_requested.connect(self.open_menu)
        self.plot_item.hideButtons()
        self.plot.setBackground(tk.CHART_BG)
        self.plot.getPlotItem().legend.hide()
        self.plot.getViewBox().sigRangeChanged.connect(self.position_labels)

    def reset(self) -> None:
        for menu in self.plot.findChildren(QMenu):
            menu.close()
        super().reset()
        self.plot_item.hideButtons()
        self.plot.disableAutoRange()
        self.overlays.clear()
        self.readings.clear()
        self.trace = None
        self.value.hide()

    def build_menu(self, position):
        menu = QMenu(self.plot)
        menu.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        frame = self.displayed_frame
        fit = menu.addAction("适应 Y 轴", self.fit_current)
        restore = menu.addAction("恢复 Y 轴（全部）", self.trace_view.restore)
        fit.setEnabled(frame is not None)
        restore.setEnabled(frame is not None)
        menu.addSeparator()
        box = self.plot.getViewBox()
        scene_pos = self.plot.mapToScene(position)
        frequency = box.mapSceneToView(scene_pos).x()
        marker = menu.addAction("在此频率添加标记")
        marker.setEnabled(
            bool(
                self.trace
                and self.trace.axis_unit == "Hz"
                and box.rect().contains(box.mapFromScene(scene_pos))
            )
        )
        marker.triggered.connect(lambda: self.add_frequency.emit(frequency))
        menu.addSeparator()
        save = menu.addAction("保存当前帧数据", lambda: self.save_frame.emit(frame))
        save.setEnabled(frame is not None and self.save_enabled)
        snapshot = self.plot.export_snapshot()
        png = menu.addAction("导出 PNG", lambda: self.plot.save_image(snapshot))
        png.setEnabled(frame is not None and bool(snapshot.curves))
        return menu

    def open_menu(self, position):
        self.build_menu(position).popup(self.plot.viewport().mapToGlobal(position))

    def set_markers(self, markers: list[SpectrumMarker]) -> None:
        self.markers = list(markers)
        for items in self.overlays.values():
            for item in items:
                self.plot.removeItem(item)
        self.overlays.clear()
        for marker in markers:
            line = MarkerLine(
                angle=90, movable=False, pen=pg.mkPen(tk.ACCENT, width=1, style=Qt.PenStyle.DashLine)
            )
            label = MarkerLabel(color=tk.TEXT, anchor=(0.5, 1), fill=tk.SURFACE_2, border=tk.BORDER_STRONG)
            point = pg.ScatterPlotItem(size=7, brush=tk.ACCENT, pen=None)
            line.edit_requested.connect(lambda n=marker.number: self.edit_marker.emit(n))
            label.edit_requested.connect(lambda n=marker.number: self.edit_marker.emit(n))
            line.setZValue(10)
            label.setZValue(11)
            point.setZValue(10)
            for item in (line, label, point):
                self.plot.addItem(item, ignoreBounds=True)
            self.overlays[marker.number] = (line, label, point)
        self.update_markers()

    def display_measurement(self, measurement):
        scale = measurement.metadata.get("spectrum_display")
        if scale and scale["unit"] != measurement.unit:
            measurement = replace(
                measurement,
                values=display_values(measurement.values, measurement.unit, scale),
                unit=scale["unit"],
            )
        self.trace = measurement
        return measurement

    def show_frame(self, frame, source, channels) -> None:
        previous = self.last_token
        super().show_frame(frame, source, channels)
        if self.last_token == previous:
            return
        self.meta.setToolTip(self.meta.text())
        stamp = datetime.fromisoformat(frame.batch.timestamp).astimezone()
        self.meta.setText(f"最新频谱 · {stamp:%H:%M:%S}")
        if self.displayed_frame is None:
            self.trace = None
        if self.trace and self.trace.metadata.get("axis_kind") == "transfer_sample_index":
            settings = self.trace.metadata.get("settings", {})
            start, stop = settings.get("start_frequency"), settings.get("stop_frequency")
            if isinstance(start, (float, int)) and isinstance(stop, (float, int)):
                self.meta.setText(
                    f"仪器扫频范围 {frequency_text(start)} – {frequency_text(stop)} · 横轴为回包样本序号"
                )
        if self.trace and self.trace.axis_unit in {"Hz", "s"}:
            if (
                self.trace.metadata.get("settings", {}).get("sweep_mode_source")
                == "operator_confirmed_linear"
            ):
                self.meta.setText(self.meta.text() + " · 面板确认线性扫描")
        self.amplitude_unit = self.trace.unit if self.trace else ""
        self.update_markers()

    def update_markers(self) -> None:
        self.readings = {}
        for marker in self.markers:
            m = self.trace
            reading = (
                locate_marker(marker, m.axis, m.values, m.axis_unit)
                if m is not None and m.axis is not None
                else MarkerReading("无有效数据")
            )
            self.readings[marker.number] = reading
            line, label, point = self.overlays[marker.number]
            visible = reading.position is not None
            for item in (line, label, point):
                item.setVisible(visible)
            if visible:
                assert reading.position is not None and reading.sample_frequency is not None
                line.setValue(reading.position)
                label.setText(f"M{marker.number} · {frequency_text(reading.position)}")
                label.setToolTip(
                    f"采样频率：{frequency_text(reading.sample_frequency)}\n"
                    f"{reading.amplitude:.8g} {self.amplitude_unit}"
                )
                point.setData([reading.position], [reading.amplitude])
        self.position_labels()
        self.readings_changed.emit()

    def position_labels(self, *_args) -> None:
        if not self.overlays:
            return
        (left, right), (bottom, top) = self.plot.viewRange()
        width = max(1, self.plot.getViewBox().width())
        last_right = [-1e9, -1e9, -1e9]
        for number, reading in sorted(self.readings.items(), key=lambda p: p[1].position or 0):
            if reading.position is None:
                continue
            label = self.overlays[number][1]
            pixels = (reading.position - left) / max(right - left, 1e-15) * width
            half = label.boundingRect().width() / 2
            pixels = max(half, min(width - half, pixels))
            lane = next((i for i, edge in enumerate(last_right) if edge < pixels - half - 4), 2)
            last_right[lane] = pixels + half
            height = max(1, self.plot.getViewBox().height())
            offset = 4 + lane * (label.boundingRect().height() + 4)
            label.setPos(left + pixels / width * (right - left), bottom + (top - bottom) * offset / height)
