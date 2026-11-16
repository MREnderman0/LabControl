"""Spectrum viewport and the menu surface shared by its plot and axes."""

import math

from PySide6.QtCore import QPoint, Qt, Signal

from lab_control.ui.pages.monitoring.trace_view import TraceView
from lab_control.ui.widgets.plot_export import PublicationPlotWidget


class SpectrumCanvas(PublicationPlotWidget):
    menu_requested = Signal(QPoint)

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
        else:
            self.plot.setYRange(*self.panel_y, padding=0)
