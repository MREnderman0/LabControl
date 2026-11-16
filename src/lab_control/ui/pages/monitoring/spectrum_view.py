"""Spectrum viewport and the menu surface shared by its plot and axes."""

import math

import pyqtgraph as pg
from PySide6.QtCore import QPoint, Qt, Signal

from lab_control.ui.pages.monitoring.trace_view import TraceView
from lab_control.ui.widgets.plot_export import PublicationPlotWidget


class SpectrumAmplitudeAxis(pg.AxisItem):
    def __init__(self, label_margin):
        self.label_margin = label_margin
        super().__init__("left")

    def tickValues(self, minVal, maxVal, size):  # noqa: N802
        levels = super().tickValues(minVal, maxVal, size)
        if not levels or self.logMode or minVal == maxVal:
            return levels
        low, high = sorted((minVal, maxVal))
        # Only major labels are guaranteed space. Promote both ends even when
        # they fall between regular ticks, keeping the interior grid intact.
        levels = [(step, [v for v in ticks if low < v < high]) for step, ticks in levels]
        major = levels[0][1]
        major.insert(0, low)
        major.append(high)
        return levels

    def tickStrings(self, values, scale, spacing):  # noqa: N802
        labels = super().tickStrings(values, scale, spacing)
        if self.logMode:
            return labels
        low, high = sorted(self.range)
        gap = (high - low) * (2 * self.label_margin) / max(1, self.height())
        ends = {v: f"{v * scale:.8g}" for v in (low, high)}
        if ends[low] == ends[high] and low != high:
            ends = {v: str(v * scale) for v in (low, high)}
        for i, value in enumerate(values):
            if value in ends:
                # Grid spacing may be integral while an instrument limit is fractional.
                labels[i] = ends[value]
            elif min(value - low, high - value) < gap:
                labels[i] = None  # AxisItem keeps this grid line but skips its text.
        return labels

    def boundingRect(self):  # noqa: N802
        # With the grid enabled, AxisItem clips labels to the ViewBox ends.
        return super().boundingRect().adjusted(0, -self.label_margin, 0, self.label_margin)


class SpectrumCanvas(PublicationPlotWidget):
    menu_requested = Signal(QPoint)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        margin = math.ceil(self.fontMetrics().height() / 2) + 1
        self.plotItem.setAxisItems({"left": SpectrumAmplitudeAxis(margin)})
        self.plotItem.layout.setContentsMargins(1, margin, 1, 1)

    def mousePressEvent(self, event):  # noqa: N802
        if event.button() == Qt.MouseButton.RightButton:
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):  # noqa: N802
        if event.button() == Qt.MouseButton.RightButton:
            self.menu_requested.emit(event.position().toPoint())
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    def contextMenuEvent(self, event):  # noqa: N802
        event.accept()


class SpectrumView(TraceView):
    def __init__(self, plot):
        super().__init__(plot)
        box = plot.getViewBox()
        box.setMouseMode(box.PanMode)
        box.setMouseEnabled(x=False, y=True)
        box.sigRangeChangedManually.connect(self._manual_zoom)

    def reset(self):
        super().reset()
        self.follow_panel = True
        self.panel_y = None
        self.default_dbm: tuple[float | None, float | None] = (None, None)
        self.unit_key = None

    def _manual_zoom(self, axes):
        if axes[1]:
            self.follow_panel = False

    def update(self, frame, source_key, channels, points, *, waveform=False):
        if not points:
            return
        self.active = True
        self.points = points
        trace = frame.batch.measurements[0]
        config = frame.batch.configuration
        display = trace.metadata.get("spectrum_display") or {}
        unit_key = (frame.batch.identity.key, trace.unit, display.get("unit", trace.unit))
        if unit_key != self.unit_key:
            self.follow_panel = True
            self.unit_key = unit_key

        start, stop = config.get("start_frequency"), config.get("stop_frequency")
        if trace.axis_unit == "Hz" and self._valid_range(start, stop):
            left, right = start, stop
        else:
            x = points[0][0]
            left, right = float(x.min()), float(x.max())
        if left == right:
            left, right = self.padded(left, right, 0.02)
        self.plot.disableAutoRange()
        if self.axis_key != (left, right):
            self.plot.setXRange(left, right, padding=0)
            self.axis_key = (left, right)

        lo, hi = display.get("lower"), display.get("upper")
        self.panel_y = (lo, hi) if self._valid_range(lo, hi) else None
        if self.follow_panel:
            self.restore()

    @staticmethod
    def _valid_range(lo, hi):
        return all(isinstance(v, (int, float)) and math.isfinite(v) for v in (lo, hi)) and lo < hi

    def fit(self):
        """Fit once to the complete frame, including narrow peaks."""
        if self.points:
            self.follow_panel = False
            self._fit_data()

    def _fit_data(self):
        bounds = self.bounds(self.points)
        if bounds is not None:
            self.plot.setYRange(*self.padded(bounds[2], bounds[3], 0.1), padding=0)

    def restore(self):
        self.follow_panel = True
        if self.panel_y is None:
            self._fit_data()
            return
        lower, upper = self.panel_y
        if self.unit_key[-1] == "dBm":
            bottom, top = self.default_dbm
            lower = lower if bottom is None else bottom
            upper = upper if top is None else top
            if lower >= upper:
                # A reference level can be set below the preferred display floor.
                lower, upper = self.panel_y
        self.plot.setYRange(lower, upper, padding=0)
