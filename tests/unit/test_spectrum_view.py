from dataclasses import replace

import numpy as np
import pytest

from lab_control.application.acquisition_pipeline import immutable_batch
from lab_control.application.controls import DataSource
from lab_control.application.monitoring import MonitorFrame
from lab_control.domain.instruments import AcquisitionRequest, DeviceIdentity, Measurement, MeasurementBatch
from lab_control.instruments.registry import DEVICE_DEFINITIONS
from lab_control.ui.pages.monitoring.plot import LivePlot
from lab_control.ui.pages.monitoring.spectrum_markers import SpectrumPlot


def frame(y=(-90.0, -10.0), *, version=0, config=None, unit="dBm", axis_unit="Hz", session="one", x=None):
    batch = MeasurementBatch(
        DeviceIdentity("Mock", "Spectrum", "fixture"),
        (
            Measurement(
                "spectrum",
                unit,
                np.array(y),
                axis=np.array(x if x is not None else [100.0, 200.0]),
                axis_unit=axis_unit,
            ),
        ),
        config or {"span": 100},
        validation="cached",
    )
    return MonitorFrame(
        1, version, AcquisitionRequest("spectrum"), immutable_batch(batch), session_id=session
    )


def test_generic_trace_holds_axis_then_refits_once_on_settings_or_manual_fit(qtbot):
    plot = LivePlot()
    qtbot.addWidget(plot)
    plot.show()
    source = DataSource("spectrum", "频谱", "spectrum")
    first = frame()
    plot.show_frame(first, source, (1,))
    assert plot.plot.viewRange()[0] == pytest.approx([100, 200])
    assert plot.plot.viewRange()[1] == pytest.approx([-98, -2])
    plot.show_frame(frame((-160, 40)), source, (1,))
    qtbot.wait(30)
    assert plot.plot.viewRange()[1] == pytest.approx([-98, -2])
    assert plot.plot.getViewBox().state["autoRange"] == [False, False]
    plot.plot.plotItem.autoBtnClicked()  # The built-in Fit must follow the same one-shot policy.
    assert plot.plot.viewRange()[1] == pytest.approx([-180, 60])
    plot.show_frame(frame((-70, -60)), source, (1,))
    assert plot.plot.viewRange()[1] == pytest.approx([-180, 60])
    plot.show_frame(frame((-70, -60), version=1), source, (1,))
    assert plot.plot.viewRange()[1] == pytest.approx([-71, -59])
    plot.plot.setYRange(-100, 0, padding=0)
    plot.plot.setXRange(120, 140, padding=0)
    # Recording/refresh timestamps must not reset a manual viewport.
    changed = frame((-150, 20), version=1, config={"span": 100, "monitor_record_session": "new"})
    plot.show_frame(
        replace(changed, batch=replace(changed.batch, settings_timestamps={"span": "new-time"})), source, (1,)
    )
    assert plot.plot.viewRange()[1] == pytest.approx([-100, 0])
    assert plot.plot.viewRange()[0] == pytest.approx([120, 140])
    # Same session version after a front-panel refresh, but different actual settings.
    plot.show_frame(frame((-90, -40), version=1, config={"span": 100, "rbw": 3000}), source, (1,))
    assert plot.plot.viewRange()[1] == pytest.approx([-95, -35])


def test_invalid_frame_does_not_consume_fit_and_units_zero_span_reconnect_reset(qtbot):
    plot = LivePlot()
    qtbot.addWidget(plot)
    plot.show()
    source = DataSource("spectrum", "频谱", "spectrum")
    original = frame()
    plot.show_frame(original, source, (1,))
    plot.show_frame(frame((np.nan, 2), version=1), source, (1,))
    assert plot.displayed_frame.batch.timestamp == original.batch.timestamp
    assert plot.plot.viewRange()[1] == pytest.approx([-98, -2])
    plot.show_frame(frame((0, 0), version=1, unit="V", axis_unit="s", x=[0, 0.2]), source, (1,))
    assert plot.plot.viewRange()[1][0] < 0 < plot.plot.viewRange()[1][1]
    assert plot.plot.viewRange()[0] == pytest.approx([0, 0.2])
    plot.show_frame(
        frame((1, 2), version=1, unit="V", axis_unit="s", x=[0, 0.2], session="two"), source, (1,)
    )
    assert plot.plot.viewRange()[1] == pytest.approx([0.9, 2.1])
    plot.reset()
    assert plot.displayed_frame is None


def test_spectrum_panel_follow_zoom_fit_and_restore(qtbot):
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QWheelEvent
    from PySide6.QtWidgets import QApplication

    plot = SpectrumPlot()
    qtbot.addWidget(plot)
    plot.resize(600, 350)
    plot.show()
    source = DataSource("spectrum", "频谱", "spectrum")

    def show(top, fmin=95.0, fmax=205.0, unit="dBm"):
        f = frame(config={"start_frequency": fmin, "stop_frequency": fmax}, unit=unit)
        m = f.batch.measurements[0]
        if top is not None:
            m = replace(m, metadata={"spectrum_display": {"unit": unit, "upper": top, "lower": top - 100}})
        plot.show_frame(replace(f, batch=replace(f.batch, measurements=(m,))), source, (1,))

    show(-10)
    assert plot.plot.viewRange()[0] == pytest.approx([95, 205])
    assert plot.plot.viewRange()[1] == pytest.approx([-110, -10])
    qtbot.wait(10)
    viewport = plot.plot.viewport()
    pos = viewport.rect().center()
    event = QWheelEvent(
        QPointF(pos),
        QPointF(viewport.mapToGlobal(pos)),
        QPoint(),
        QPoint(0, 120),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    QApplication.sendEvent(viewport, event)
    zoom = plot.plot.viewRange()[1]
    assert zoom[1] - zoom[0] < 100
    assert plot.plot.viewRange()[0] == pytest.approx([95, 205])
    show(-30, 90.0, 210.0)
    assert plot.plot.viewRange()[0] == pytest.approx([90, 210])
    assert plot.plot.viewRange()[1] == pytest.approx(zoom)
    plot.fit_current()
    assert plot.plot.viewRange()[1] == pytest.approx([-98, -2])
    show(-40)
    assert plot.plot.viewRange()[1] == pytest.approx([-98, -2])
    fixed_x = plot.plot.viewRange()[0]
    restore = next(a for a in plot.build_menu(pos).actions() if a.text() == "恢复视图")
    restore.trigger()
    assert plot.plot.viewRange()[0] == pytest.approx(fixed_x)
    assert plot.plot.viewRange()[1] == pytest.approx([-140, -40])
    show(None)
    assert plot.trace_view.panel_y is None
    show(-15)
    assert plot.plot.viewRange()[1] == pytest.approx([-115, -15])
    plot.fit_current()
    show(5, unit="dBmV")
    assert plot.plot.viewRange()[1] == pytest.approx([-95, 5])


@pytest.mark.parametrize(
    "adapter, panel, expected",
    [
        ("keysight.n9030a", (-88, 12), (-120, 12)),
        ("ceyear.4959e", (-60, 40), (-60, 30)),
        ("rigol.dsa815", (-91, 9), (-80, 9)),
        ("rigol.dsa815", (-190, -90), (-190, -90)),
        ("rs.fph", (-110, -10), (-110, -10)),
    ],
)
def test_model_dbm_view_on_first_frame_fit_and_restore(qtbot, adapter, panel, expected):
    plot = SpectrumPlot()
    qtbot.addWidget(plot)
    plot.trace_view.default_dbm = DEVICE_DEFINITIONS[adapter].spectrum_view_dbm
    source = DataSource("spectrum", "频谱", "spectrum")

    def show(unit, version):
        f = frame(y=(-115, -5), unit=unit, version=version)
        m = f.batch.measurements[0]
        scale = {"lower": panel[0], "upper": panel[1], "unit": unit}
        m = replace(m, metadata={"spectrum_display": scale})
        plot.show_frame(replace(f, batch=replace(f.batch, measurements=(m,))), source, (1,))

    show("dBm", 0)
    assert plot.plot.viewRange()[1] == pytest.approx(expected)
    assert plot.trace_view.panel_y == panel
    plot.fit_current()
    fitted = (-126, 6)
    assert plot.plot.viewRange()[1] == pytest.approx(fitted)
    show("dBm", 1)
    assert plot.plot.viewRange()[1] == pytest.approx(fitted)
    plot.trace_view.restore()
    assert plot.plot.viewRange()[1] == pytest.approx(expected)
    show("dBmV", 2)
    assert plot.plot.viewRange()[1] == pytest.approx(panel)


@pytest.mark.parametrize(
    "size, bounds",
    [
        ((700, 420), (-120, 30)),
        ((480, 260), (-120, 12)),  # N9030A
        ((580, 320), (-60, 30)),  # 4959E
        ((520, 280), (-80, 9)),  # DSA815
        ((420, 220), (-88.25, 12.125)),  # Neither limit is a regular grid tick.
    ],
)
def test_dbm_endpoint_labels_are_drawn_with_the_grid(qtbot, size, bounds):
    from PySide6.QtGui import QImage, QPainter

    plot = SpectrumPlot()
    qtbot.addWidget(plot)
    plot.resize(*size)
    plot.show()
    source = DataSource("spectrum", "频谱", "spectrum")
    f = frame()
    m = replace(
        f.batch.measurements[0],
        metadata={"spectrum_display": {"unit": "dBm", "lower": bounds[0], "upper": bounds[1]}},
    )
    plot.show_frame(replace(f, batch=replace(f.batch, measurements=(m,))), source, (1,))
    qtbot.wait(20)
    axis = plot.plot.getAxis("left")
    viewport = plot.plot.viewport()
    image = QImage(plot.size(), QImage.Format.Format_ARGB32)
    with QPainter(image) as painter:
        _, _, specs = axis.generateDrawSpecs(painter)

    assert plot.plot.viewRange()[1] == pytest.approx(bounds)
    areas = []
    for limit, edge in zip(bounds, (axis.rect().bottom(), axis.rect().top()), strict=True):
        label = str(limit)
        (rect,) = [rect for rect, _, text in specs if text == label]
        assert rect.center().y() == pytest.approx(edge)
        assert not any(rect.intersects(other) for other, _, text in specs if text != label)
        area = plot.plot.mapFromScene(axis.mapRectToScene(rect)).boundingRect()
        assert viewport.rect().contains(area)
        areas.append(area)

    # Verify the labels actually reach the viewport. Grid lines remain in both grabs.
    visible = [viewport.grab(area).toImage() for area in areas]
    axis.setTextPen((0, 0, 0, 0))
    for area, labeled in zip(areas, visible, strict=True):
        assert labeled != viewport.grab(area).toImage()


def test_menu_marker_uses_clicked_frequency_and_requires_a_frequency_position(qtbot):
    from PySide6.QtCore import QPointF

    plot = SpectrumPlot()
    qtbot.addWidget(plot)
    plot.resize(680, 380)
    plot.show()
    source = DataSource("spectrum", "频谱", "spectrum")
    plot.show_frame(frame(), source, (1,))
    qtbot.wait(10)
    box = plot.plot.getViewBox()
    position = plot.plot.mapFromScene(box.mapViewToScene(QPointF(167.5, -50)))
    expected = box.mapSceneToView(plot.plot.mapToScene(position)).x()

    def marker_action(position):
        return next(a for a in plot.build_menu(position).actions() if a.text() == "在此频率添加标记")

    marker = marker_action(position)
    assert marker.isEnabled()
    with qtbot.waitSignal(plot.add_frequency) as signal:
        marker.trigger()
    assert signal.args == pytest.approx([expected])
    assert expected == pytest.approx(167.5, abs=0.3)

    for side in ("bottom", "left"):
        axis = plot.plot_item.getAxis(side)
        center = axis.mapToScene(axis.rect().center())
        assert not marker_action(plot.plot.mapFromScene(center)).isEnabled()
    for unit in ("s", "index"):
        plot.show_frame(frame(axis_unit=unit, x=[0, 1]), source, (1,))
        assert not marker_action(position).isEnabled()


def test_snapshot_retains_selected_full_arrays_and_other_plot_autorange(qtbot):
    plot = LivePlot()
    qtbot.addWidget(plot)
    source = DataSource("magnitude", "网络分析", "s_parameters")
    original = frame(y=np.arange(10001), x=np.arange(10001))
    original = replace(
        original,
        batch=replace(
            original.batch,
            measurements=(
                original.batch.measurements[0],
                replace(original.batch.measurements[0], channel=2),
            ),
        ),
    )
    plot.show_frame(original, source, (2,))
    assert plot.plot.getViewBox().state["autoRange"] == [True, True]
    assert len(plot.displayed_frame.batch.measurements) == 1
    assert plot.displayed_frame.batch.measurements[0].channel == 2
    assert plot.displayed_frame.batch.measurements[0].values.size == 10001
    assert not plot.displayed_frame.batch.measurements[0].values.flags.writeable
