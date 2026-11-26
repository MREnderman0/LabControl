from dataclasses import replace

import numpy as np
import pytest

from lab_control.application.monitoring import MonitorFrame
from lab_control.domain.instruments import AcquisitionRequest, DeviceIdentity, Measurement, MeasurementBatch
from lab_control.ui.pages.monitoring.power_plot import PowerPlot


def frame(sequence, elapsed, voltage=1.0, current=0.2, *, session="one", version=0):
    return MonitorFrame(
        sequence,
        version,
        AcquisitionRequest("scalar", (1,), points=1),
        MeasurementBatch(
            DeviceIdentity("Mock", "Power"),
            (Measurement("voltage", "V", [voltage], 1), Measurement("current", "A", [current], 1)),
            {},
        ),
        received_at=elapsed,
        session_id=session,
    )


def test_actual_irregular_times_stable_axes_and_expansion(qtbot):
    plot = PowerPlot()
    qtbot.addWidget(plot)
    first = frame(1, 100, voltage=2)
    assert plot.show_frame(first, (1,))
    initial = plot.plots["voltage"].viewRange()[1]
    assert initial[0] < 2 < initial[1]
    assert plot.show_frame(frame(2, 101.7, voltage=2.001), (1,))
    assert plot.plots["voltage"].viewRange()[1] == initial
    np.testing.assert_allclose(plot.curves[(1, "voltage")].xData, [0, 1.7])
    # A configuration change preserves the history; no inferred timer timestamps.
    assert plot.show_frame(frame(3, 105.1, voltage=5, version=1), (1,))
    expanded = plot.plots["voltage"].viewRange()[1]
    assert expanded[0] <= initial[0] and expanded[1] > 5
    assert plot.show_frame(frame(4, 705, voltage=5, version=1), (1,))
    assert plot.plots["voltage"].viewRange()[1] == expanded
    assert len(plot.history[(1, "voltage")]) == 2
    np.testing.assert_allclose(plot.curves[(1, "voltage")].xData, [5.1, 605])
    np.testing.assert_allclose(plot.plots["voltage"].viewRange()[0], [5, 605])
    plot.fit_current()
    assert plot.plots["voltage"].viewRange()[1][0] > 4
    assert plot.plots["current"].viewRange()[1][1] < 1


def test_invalid_frame_keeps_displayed_snapshot_and_new_session_resets(qtbot):
    plot = PowerPlot()
    qtbot.addWidget(plot)
    valid = frame(1, 100)
    assert plot.show_frame(valid, (1,))
    before = plot.displayed_frame
    assert not plot.show_frame(frame(2, 102, voltage=float("nan")), (1,))
    assert plot.displayed_frame is before
    wrong = frame(3, 103)
    wrong = replace(wrong, batch=replace(wrong.batch, measurements=(wrong.batch.measurements[0],)))
    assert not plot.show_frame(wrong, (1,))
    assert plot.displayed_frame is before
    assert plot.show_frame(frame(1, 200, session="new"), (1,))
    assert len(plot.history[(1, "voltage")]) == 1
    assert plot.time_origin == 200
    np.testing.assert_allclose(plot.curves[(1, "voltage")].xData, [0])
    plot.set_channels(())
    assert plot.displayed_frame is None
    assert all(not curve.isVisible() for curve in plot.curves.values())


@pytest.mark.parametrize("pan, zoom", [(True, False), (False, True), (True, True)])
def test_restore_shared_time_and_both_y_ranges_after_pan_or_zoom(qtbot, pan, zoom):
    plot = PowerPlot()
    qtbot.addWidget(plot)
    plot.show()
    plot.show_frame(frame(1, 100, voltage=100), (1,))
    plot.show_frame(frame(2, 650, voltage=2), (1,))
    voltage, current = plot.plots["voltage"], plot.plots["current"]
    box = current.getViewBox()
    if pan:
        box.translateBy(x=250)
    if zoom:
        box.scaleBy(x=0.25)
    manual = voltage.viewRange()[0]
    assert manual != [0, 600]
    plot.show_frame(frame(3, 800, voltage=4), (1,))
    assert voltage.viewRange()[0] == pytest.approx(manual)
    for canvas in (voltage, current):
        canvas.setYRange(70, 90, padding=0)

    current.build_menu().actions()[0].trigger()
    for canvas in (voltage, current):
        assert canvas.viewRange()[0] == pytest.approx([100, 700])
    assert voltage.viewRange()[1] == pytest.approx([1.8, 4.2])
    assert current.viewRange()[1] == pytest.approx([0.198, 0.202])
    plot.show_frame(frame(4, 851, voltage=3), (1,))
    for canvas in (voltage, current):
        assert canvas.viewRange()[0] == pytest.approx([151, 751])


def test_time_origin_starts_at_first_valid_frame_and_never_rebases(qtbot):
    plot = PowerPlot()
    qtbot.addWidget(plot)
    plot.show()
    assert not plot.show_frame(frame(0, 5, voltage=float("nan")), (1,))
    assert plot.time_origin is None
    first = frame(1, 10)
    assert plot.show_frame(first, (1,))
    assert plot.plots["voltage"].viewRange()[0] == [0, 600]
    np.testing.assert_allclose(plot.curves[(1, "voltage")].xData, [0])
    # A pause is a real elapsed-time gap, not synthesized interval-sized samples.
    plot.show_frame(frame(2, 30.2), (1,))
    np.testing.assert_allclose(plot.curves[(1, "voltage")].xData, [0, 20.2])
    plot.show_frame(frame(3, 620.5), (1,))
    np.testing.assert_allclose(plot.curves[(1, "voltage")].xData, [20.2, 610.5])
    for chart in plot.plots.values():
        np.testing.assert_allclose(chart.viewRange()[0], [10.5, 610.5])
    assert plot.time_origin == 10
