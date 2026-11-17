"""Oscilloscope screen tags: 0 V / trigger positions, drag mapping and software measurements."""

from dataclasses import replace

import numpy as np
import pytest

from lab_control.domain.oscilloscope import ScopeDisplay
from lab_control.ui.pages.monitoring.oscilloscope_plot import OscilloscopePlot, waveform_measurements

from .test_oscilloscope_grid import SOURCE, frame


def offset_frame(sign: int, position: float = 0.0):
    sample = frame(channels=(1, 2))
    measurements = []
    for m in sample.batch.measurements:
        display = ScopeDisplay(
            0.01 * m.channel,
            0.02,
            1e-6,
            0,
            vertical_offset_sign=sign,
            vertical_position_divisions=position,
        )
        measurements.append(replace(m, metadata={"scope_display": display.metadata()}))
    return replace(sample, batch=replace(sample.batch, measurements=tuple(measurements)))


@pytest.fixture
def plot(qtbot):
    widget = OscilloscopePlot()
    widget.resize(900, 500)
    qtbot.addWidget(widget)
    widget.show()
    return widget


@pytest.mark.parametrize("sign,position", [(1, 0.0), (-1, 0.0), (1, 1.5)])
def test_ground_tag_drag_maps_back_to_instrument_offset(plot, sign, position):
    plot.show_frame(offset_frame(sign, position), SOURCE, (1, 2))
    plot.set_drag_state(frozenset({1}), (1, 0.005, True))
    tag = plot.ground_tags[1]
    assert tag.draggable and not plot.ground_tags[2].draggable
    assert tag.pos().y() == pytest.approx(-2.0)  # 0 V sits at -reference / scale
    emitted = []
    plot.ground_dragged.connect(lambda *args: emitted.append(args))
    tag.dragged.emit(1.0, True)
    channel, offset, finished = emitted[-1]
    assert channel == 1 and finished
    # Re-deriving the 0 V level from the written offset must land on the dropped position.
    spec = plot.display_specs[1]
    reference = sign * offset - position * spec.units_per_div
    assert -reference / spec.units_per_div == pytest.approx(1.0)


def test_trigger_tag_follows_level_and_reports_volts(plot):
    plot.show_frame(offset_frame(1), SOURCE, (1, 2))
    plot.set_drag_state(frozenset(), (2, 0.0, True))
    tag = plot.trigger_tag
    assert tag is not None and tag.isVisible() and tag.draggable
    assert tag.pos().y() == pytest.approx((0.0 - 0.02) / 0.02)
    levels = []
    plot.trigger_dragged.connect(lambda level, done: levels.append((level, done)))
    tag.dragged.emit(2.0, False)
    assert levels == [(pytest.approx(2.0 * 0.02 + 0.02), False)]
    plot.set_drag_state(frozenset(), (None, None, False))
    assert not tag.isVisible()


def test_tags_are_not_draggable_without_an_offset_convention(plot):
    plot.show_frame(offset_frame(1), SOURCE, (1, 2))
    plot.set_drag_state(frozenset({1, 2}), (1, 0.0, False))
    assert plot.ground_tags[1].draggable and not plot.trigger_tag.draggable
    plain = frame(channels=(1,))
    plot.show_frame(replace(plain, sequence=9), SOURCE, (1,))
    assert not plot.ground_tags[1].draggable  # vertical_offset_sign unknown


def test_software_measurements_and_noise_has_no_frequency():
    t = np.linspace(0, 1e-3, 20_001)
    vpp, rms, frequency = waveform_measurements(1.5 * np.sin(2 * np.pi * 10e3 * t) + 0.2, t)
    assert vpp == pytest.approx(3.0, rel=1e-3)
    assert rms == pytest.approx(np.sqrt(1.5**2 / 2 + 0.2**2), rel=1e-3)
    assert frequency == pytest.approx(10e3, rel=1e-3)
    noise = np.random.default_rng(1).normal(0, 1, t.size)
    assert waveform_measurements(noise, t)[2] is None
    assert waveform_measurements(np.full(10, np.nan), np.arange(10.0))[2] is None


@pytest.mark.parametrize("frequency", [199_000, 7.5])
def test_native_crossings_across_blocks(frequency):
    t = np.arange(1_000_000) / 1_000_000
    trace = np.sin(2 * np.pi * frequency * t + 0.3)
    assert waveform_measurements(trace, t)[2] == pytest.approx(frequency, rel=0.01)
    assert waveform_measurements(np.ones(100), t[:100])[2] is None
    assert waveform_measurements(trace[:8], t[:8])[2] is None
