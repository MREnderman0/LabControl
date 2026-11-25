"""Local display scaling. Scope coordinates and stored samples retain their original units."""

import math

from PySide6.QtCore import QSignalBlocker
from PySide6.QtWidgets import QDoubleSpinBox

from lab_control.domain.instruments import Parameter
from lab_control.domain.oscilloscope import ScopeDisplay
from lab_control.ui.kit import Switch
from lab_control.ui.pages.monitoring.oscilloscope_controls import ScopeSlider
from lab_control.ui.pages.monitoring.oscilloscope_plot import OscilloscopePlot
from lab_control.ui.pages.monitoring.spectrum_markers import SpectrumPlot
from lab_control.ui.pages.monitoring.spectrum_view import SpectrumView


class MokuScopePlot(OscilloscopePlot):
    vertical = None

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
        self.auto = Switch("自动")
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
        if self.sliders and self.offset is not None:
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
