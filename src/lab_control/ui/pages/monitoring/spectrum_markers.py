"""Local spectrum overlays; all calculations use the full, un-decimated trace."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Any

import pyqtgraph as pg
from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QFontMetrics, QGradient, QLinearGradient
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from lab_control.domain.instruments import Parameter
from lab_control.domain.spectrum import MarkerReading, SpectrumMarker, locate_marker
from lab_control.domain.spectrum_display import display_values
from lab_control.ui.kit import style_button
from lab_control.ui.kit.numeric import fit_reading, number_text
from lab_control.ui.pages.device_control.editors import ParameterEditor
from lab_control.ui.pages.monitoring.plot import LivePlot
from lab_control.ui.pages.monitoring.spectrum_view import SpectrumCanvas, SpectrumView
from lab_control.ui.styles import tokens as tk
from lab_control.ui.widgets.popup_surface import WorkspaceMenu

MARKER_COLORS = (tk.ACCENT, tk.INFO, tk.CHANNEL_COLORS[3], tk.CHANNEL_COLORS[6])


def frequency_text(value: float) -> str:
    scale, unit = next(
        ((s, u) for s, u in ((1e9, "GHz"), (1e6, "MHz"), (1e3, "kHz")) if abs(value) >= s), (1, "Hz")
    )
    return f"{number_text(value / scale)} {unit}"


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
        self.last_frame_clock = ""
        self.save_enabled = False
        self.meta.hide()
        self.plot.menu_factory = self.build_menu
        self.plot_item.hideButtons()
        self.plot.setBackground(tk.CHART_BG)
        self.plot.showGrid(x=True, y=True, alpha=0.14)
        self.plot.getPlotItem().legend.hide()
        self.plot.getAxis("left").setWidth(44)
        self.plot.getViewBox().sigRangeChanged.connect(self._on_view_range_changed)
        self.trace_view.on_follow_changed = self._on_follow_changed

        # Floating manual-scale notice matching qt-demo-a.
        self.zoom_chip = QFrame(self.plot)
        self.zoom_chip.setObjectName("zoomChip")
        zc = QHBoxLayout(self.zoom_chip)
        zc.setContentsMargins(10, 3, 4, 3)
        zc.setSpacing(6)
        zc_text = QLabel("手动刻度 · 未跟随参考电平", self.zoom_chip)
        zc_text.setObjectName("zoomChipText")
        zc.addWidget(zc_text)
        self.restore_button = QPushButton("恢复跟随", self.zoom_chip)
        style_button(self.restore_button, "ghost", "scan", "sm")
        self.restore_button.clicked.connect(self.trace_view.restore)
        zc.addWidget(self.restore_button)
        self.zoom_chip.hide()
        self.plot.installEventFilter(self)

        # Subtle vertical amber area fill under the main spectrum trace.
        grad = QLinearGradient(0, 0, 0, 1)
        grad.setCoordinateMode(QGradient.CoordinateMode.ObjectMode)
        top_color, bottom_color = QColor(tk.ACCENT), QColor(tk.ACCENT)
        top_color.setAlphaF(0.18)
        bottom_color.setAlphaF(0.0)
        grad.setColorAt(0.0, top_color)
        grad.setColorAt(1.0, bottom_color)
        self._trace_fill = QBrush(grad)

    def _on_follow_changed(self, follow: bool) -> None:
        self.zoom_chip.setVisible(not follow and self.displayed_frame is not None)
        self._place_zoom_chip()

    def _place_zoom_chip(self) -> None:
        if not self.zoom_chip.isVisible():
            return
        self.zoom_chip.adjustSize()
        self.zoom_chip.move(max(12, self.plot.width() - self.zoom_chip.width() - 18), 10)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if obj is self.plot and event.type() == QEvent.Type.Resize:
            self._place_zoom_chip()
        return super().eventFilter(obj, event)

    def _on_view_range_changed(self, *_args) -> None:
        self._sync_trace_fill()
        self.position_labels()

    def _sync_trace_fill(self) -> None:
        if not self.curves:
            return
        bottom = self.plot.viewRange()[1][0]
        for curve in self.curves.values():
            curve.setFillLevel(bottom)

    def reset(self) -> None:
        for menu in self.plot.findChildren(WorkspaceMenu):
            menu.close()
        super().reset()
        self.plot_item.hideButtons()
        self.plot.disableAutoRange()
        self.overlays.clear()
        self.readings.clear()
        self.trace = None
        self.last_frame_clock = ""
        self.zoom_chip.hide()
        self.meta.hide()
        self.value.hide()

    def build_menu(self, position):
        menu = WorkspaceMenu(self.plot)
        menu.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        frame = self.displayed_frame
        fit = menu.addAction("适应 Y 轴", self.fit_current)
        restore = menu.addAction("恢复视图", self.trace_view.restore)
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

    def set_markers(self, markers: list[SpectrumMarker]) -> None:
        self.markers = list(markers)
        for items in self.overlays.values():
            for item in items:
                self.plot.removeItem(item)
        self.overlays.clear()
        marker_font = QFont(self.font())
        marker_font.setFamilies(list(tk.FONT_MONO))
        marker_font.setPixelSize(11)
        for marker in markers:
            color_hex = MARKER_COLORS[(marker.number - 1) % len(MARKER_COLORS)]
            line_color = QColor(color_hex)
            line_color.setAlpha(175)
            line = MarkerLine(
                angle=90, movable=False, pen=pg.mkPen(line_color, width=1, style=Qt.PenStyle.DashLine)
            )
            label = MarkerLabel(
                color=tk.TEXT,
                anchor=(0, 1),
                fill=pg.mkBrush(tk.SURFACE_2),
                border=pg.mkPen(tk.BORDER_STRONG),
            )
            label.setFont(marker_font)
            point = pg.ScatterPlotItem(
                symbol="d",
                size=11,
                brush=pg.mkBrush(color_hex),
                pen=pg.mkPen(tk.CHART_BG, width=1.5),
                useCache=False,
            )
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
        # Apply Graphite 1.4 px accent trace with subtle vertical area fill.
        bottom = self.plot.viewRange()[1][0]
        for curve in self.curves.values():
            curve.setPen(pg.mkPen(tk.ACCENT, width=1.4))
            curve.setBrush(self._trace_fill)
            curve.setFillLevel(bottom)
        self.meta.setToolTip(self.meta.text())
        stamp = datetime.fromisoformat(frame.batch.timestamp).astimezone()
        self.last_frame_clock = f"{stamp:%H:%M:%S}"
        self.meta.setText(f"最新频谱 · {self.last_frame_clock}")
        special_meta = False
        if self.displayed_frame is None:
            self.trace = None
        if self.trace and self.trace.metadata.get("axis_kind") == "transfer_sample_index":
            settings = self.trace.metadata.get("settings", {})
            start, stop = settings.get("start_frequency"), settings.get("stop_frequency")
            if isinstance(start, (float, int)) and isinstance(stop, (float, int)):
                self.meta.setText(
                    f"仪器扫频范围 {frequency_text(start)} – {frequency_text(stop)} · 横轴为回包样本序号"
                )
                special_meta = True
        if self.trace and self.trace.axis_unit in {"Hz", "s"}:
            if (
                self.trace.metadata.get("settings", {}).get("sweep_mode_source")
                == "operator_confirmed_linear"
            ):
                self.meta.setText(self.meta.text() + " · 面板确认线性扫描")
                special_meta = True
        self.meta.setVisible(special_meta)
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
                label.setToolTip(
                    f"采样频率：{frequency_text(reading.sample_frequency)}\n"
                    f"{number_text(reading.amplitude)} {self.amplitude_unit}"
                )
                point.setData([reading.position], [reading.amplitude])
        self.position_labels()
        self.readings_changed.emit()

    def position_labels(self, *_args) -> None:
        if not self.overlays:
            return
        (left, right), (bottom, top) = self.plot.viewRange()
        vb = self.plot.getViewBox()
        width = max(1.0, float(vb.width()))
        height = max(1.0, float(vb.height()))
        x_span = max(right - left, 1e-15)
        y_span = max(top - bottom, 1e-15)
        placed_boxes: list[tuple[float, float, float, float]] = []
        for number, reading in sorted(self.readings.items(), key=lambda p: p[1].position or 0):
            if reading.position is None or reading.amplitude is None:
                continue
            label = self.overlays[number][1]
            amplitude = number_text(reading.amplitude).replace("-", "−")
            full = f" M{number}  {frequency_text(reading.position)}  {amplitude} {self.amplitude_unit} "
            shown = fit_reading(full, max(0, int(width) - 24), QFontMetrics(label.textItem.font()))
            if label.toPlainText() != shown:
                label.setText(shown)
            rect = label.boundingRect()
            bw, bh = max(40.0, rect.width()), max(20.0, rect.height())
            px = (reading.position - left) / x_span * width
            py = (top - reading.amplitude) / y_span * height  # pixels from top
            # Place callout to the right of the peak; flip left when close to the right border.
            gap_x = 12.0
            if px + gap_x + bw <= width - 8:
                label.setAnchor((0, 0.5))
                bx0 = px + gap_x
                anchor_x = bx0
            else:
                label.setAnchor((1, 0.5))
                bx0 = max(8.0, px - gap_x - bw)
                anchor_x = bx0 + bw
            by_center = max(bh / 2 + 8.0, min(height - bh / 2 - 8.0, py))
            # Avoid overlapping previous marker callouts.
            for _ in range(4):
                by0, by1 = by_center - bh / 2, by_center + bh / 2
                conflict = next(
                    (
                        other
                        for other in placed_boxes
                        if not (
                            bx0 + bw + 4 < other[0]
                            or bx0 > other[2] + 4
                            or by1 + 4 < other[1]
                            or by0 > other[3] + 4
                        )
                    ),
                    None,
                )
                if conflict is None:
                    break
                by_center = min(height - bh / 2 - 8.0, conflict[3] + bh / 2 + 6.0)
            placed_boxes.append((bx0, by_center - bh / 2, bx0 + bw, by_center + bh / 2))
            label.setPos(left + anchor_x / width * x_span, top - by_center / height * y_span)
