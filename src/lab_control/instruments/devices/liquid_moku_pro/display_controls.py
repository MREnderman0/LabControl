"""Local display scaling. Scope coordinates and stored samples retain their original units."""

import math

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QSignalBlocker
from PySide6.QtWidgets import QCheckBox, QDoubleSpinBox

from lab_control.domain.instruments import Parameter
from lab_control.domain.oscilloscope import ScopeDisplay
from lab_control.ui.pages.monitoring.oscilloscope_controls import ScopeSlider
from lab_control.ui.pages.monitoring.oscilloscope_plot import OscilloscopePlot
from lab_control.ui.pages.monitoring.spectrum_markers import SpectrumPlot
from lab_control.ui.pages.monitoring.spectrum_view import SpectrumView


def waveform_measurements(values, axis) -> tuple[float, float, float | None]:
    """Vpp, RMS and a hysteresis-based frequency estimate of one displayed trace."""
    v = np.asarray(values, dtype=float)
    t = np.asarray(axis, dtype=float)
    finite = np.isfinite(v)
    if not finite.any():
        return float("nan"), float("nan"), None
    v_valid = v if finite.all() else v[finite]
    minimum = float(v_valid.min())
    vpp = float(v_valid.max() - minimum)
    rms = float(np.sqrt(np.mean(v_valid**2)))
    if vpp <= 0 or v.size < 8 or t.shape != v.shape or v_valid.size != v.size:
        return vpp, rms, None
    low = minimum + 0.3 * vpp
    return vpp, rms, crossing_frequency(v, t, low, low + 0.4 * vpp)


def crossing_frequency(v, t, low: float, high: float) -> float | None:
    """Time rising hysteresis crossings on native samples, carrying state across blocks."""
    was_high = True  # Arm only after observing the lower threshold.
    previous_crossing = None
    count = 0
    duration = squared_periods = 0.0
    block_size = 65_536
    for start in range(0, v.size, block_size):
        block = v[start : start + block_size]
        times = t[max(0, start - 1) : start + block.size]
        if not np.isfinite(times).all() or np.any(np.diff(times) <= 0):
            return None
        outside = np.flatnonzero((block <= low) | (block >= high))
        if not outside.size:
            continue
        states = block[outside] >= high
        rising = outside[states & ~np.r_[was_high, states[:-1]]] + start
        was_high = bool(states[-1])
        if not rising.size:
            continue
        before = rising - 1
        fraction = (high - v[before]) / (v[rising] - v[before])
        crossings = t[before] + fraction * (t[rising] - t[before])
        periods = np.diff(crossings if previous_crossing is None else np.r_[previous_crossing, crossings])
        previous_crossing = crossings[-1]
        count += periods.size
        duration += float(periods.sum())
        squared_periods += float(np.sum(periods**2))
    if count < 3 or duration <= 0:
        return None
    mean = duration / count
    # Suppress irregular crossings (noise); allow sampling jitter on short periods.
    if squared_periods / count > mean**2 * (1 + 0.15**2):
        return None
    return 1 / mean


class MokuScopePlot(OscilloscopePlot):
    vertical = None
    trigger_line = None

    def reset(self):
        super().reset()
        self.trigger_line = None

    def display_trigger(self, channel, level):
        spec = self.display_specs.get(channel)
        if spec is None:
            if self.trigger_line is not None:
                self.trigger_line.hide()
            return
        if self.trigger_line is None:
            self.trigger_line = pg.InfiniteLine(
                angle=0,
                movable=False,
                pen=pg.mkPen("#ac7c43", width=0.6),
                label="T",
                labelOpts={"position": 0.98, "color": "#deb273"},
            )
            self.plot.addItem(self.trigger_line, ignoreBounds=True)
        self.trigger_line.setPos((level - spec.reference_value) / spec.units_per_div)
        self.trigger_line.show()

    def display_spec(self, measurement, batch):
        start, stop = float(measurement.axis[0]), float(measurement.axis[-1])
        if self.vertical is None:
            low, high = float(measurement.values.min()), float(measurement.values.max())
            target = max((high - low) / 7, 1e-9)
            decade = 10 ** math.floor(math.log10(target))
            scale = next(step * decade for step in (1, 2, 5, 10) if step * decade >= target)
            center = (high + low) / 2
        else:
            scale, center = self.vertical
        return ScopeDisplay(scale, center, (stop - start) / 10, (start + stop) / 2)


class MokuSpectrumView(SpectrumView):
    reference_dbm = None

    def restore(self):
        if self.reference_dbm is None:
            super().restore()
        else:
            self.follow_panel = True
            self.plot.setYRange(self.reference_dbm - 100, self.reference_dbm, padding=0)


class MokuSpectrumPlot(SpectrumPlot):
    trace_view_type = MokuSpectrumView


class DisplayControls:
    def __init__(self, page):
        self.page = page
        self.auto = QCheckBox("自动")
        self.auto.setChecked(True)
        self.auto.setToolTip("随当前通道的数据调整显示范围")
        self.top = QDoubleSpinBox()
        self.top.setKeyboardTracking(False)
        self.offset = None
        self.sliders = []
        if page.kind == "spectrum":
            self.top.setRange(-300, 150)
            self.top.setSuffix(" dBm")
            self.top.setValue(-10)
            self.top.setToolTip("图表上边界，显示范围为 100 dB")
        else:
            self.top.setDecimals(3)
            self.top.setRange(0.001, 10_000)
            self.top.setSuffix(" mV/格")
            self.top.setValue(20)
            self.offset = QDoubleSpinBox()
            self.offset.setKeyboardTracking(False)
            self.offset.setRange(-20000, 20000)
            self.offset.setDecimals(3)
            self.offset.setSuffix(" mV")
            self.offset.valueChanged.connect(self.changed)
            for title, spin, coarse in (("档位", self.top, True), ("偏移", self.offset, False)):
                slider = ScopeSlider(title, "V", coarse=coarse)
                slider.committed.connect(lambda value, spin=spin: spin.setValue(value * 1000))
                self.sliders.append(slider)
        self.top.valueChanged.connect(self.changed)
        self.auto.toggled.connect(self.changed)
        self.preferences = {}
        self.channel = None
        self.changed()

    def frame_displayed(self):
        if not self.auto.isChecked():
            return
        if self.offset is None:
            high = self.page.plot.plot.viewRange()[1][1]
            with QSignalBlocker(self.top):
                self.top.setValue(high)
        else:
            spec = self.page.plot.display_specs.get(self.channel)
            if spec:
                with QSignalBlocker(self.top), QSignalBlocker(self.offset):
                    self.top.setValue(spec.units_per_div * 1000)
                    self.offset.setValue(spec.reference_value * 1000)
                self.update_sliders()

    def update_sliders(self):
        if self.offset is not None:
            scale = self.top.value() / 1000
            for slider, spin in zip(self.sliders, (self.top, self.offset), strict=True):
                slider.setEnabled(not self.auto.isChecked())
                slider.configure(
                    spin.value() / 1000,
                    Parameter(unit="V", minimum=spin.minimum() / 1000, maximum=spin.maximum() / 1000),
                    scale,
                )

    def bind(self, channel):
        if self.channel == channel:
            return
        self.channel = None
        auto, top, center = self.preferences.get(channel, (True, -10 if self.offset is None else 20, 0))
        self.auto.setChecked(auto)
        self.top.setValue(top)
        if self.offset is not None:
            self.offset.setValue(center)
        self.channel = channel
        self.changed()

    def changed(self):
        auto, top = self.auto.isChecked(), self.top.value()
        self.top.setEnabled(not auto)
        center = self.offset.value() if self.offset is not None else 0
        if self.offset is not None:
            self.offset.setEnabled(not auto)
            self.update_sliders()
        if self.channel is None:
            return
        self.preferences[self.channel] = auto, top, center
        plot = self.page.plot
        if self.offset is None:
            plot.trace_view.reference_dbm = None if auto else top
            plot.trace_view.restore()
        else:
            plot.vertical = None if auto else (top / 1000, center / 1000)
            if self.page.last is not None:
                plot.last_token = None
                plot.show_frame(self.page.last, self.page.source, (self.channel,))
                receipt = self.page.trigger_receipt
                if receipt and receipt[0] == self.page.state.get("sessionId"):
                    plot.display_trigger(receipt[2], receipt[1]["level"])
