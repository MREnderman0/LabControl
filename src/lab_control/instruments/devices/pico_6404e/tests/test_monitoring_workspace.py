from dataclasses import replace

import numpy as np

from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.connections import ConnectionConfig
from lab_control.domain.instruments import Quality
from lab_control.ui.pages.monitoring.oscilloscope_plot import OscilloscopePlot
from lab_control.ui.real_window import RealWindow


def test_pico_normal_workspace_prepares_inputs_once_and_saves_displayed_frame(
    qtbot, tmp_path, monkeypatch, frame_export_directory
):
    runtime = ApplicationRuntime(root=tmp_path, hardware=False)
    pico = runtime.add(ConnectionConfig("mock://pico-window", "pico.6404e", "mock"))
    scpi = runtime.add(ConnectionConfig("mock://other-scope", "lecroy.9404m", "mock"))
    runtime.connect(pico).result()
    runtime.connect(scpi).result()
    adapter = runtime.sessions.sessions[pico].adapter
    # The native Pico adapter starts with no configured channels or triggers.
    for ch in range(1, 5):
        adapter.values.pop(f"{ch}.channel", None)
        adapter.values.pop(f"{ch}.trigger", None)
    applied, requests = [], []
    overload = False
    current_apply, current_acquire = adapter.apply, adapter.acquire

    def apply(cmd):
        applied.append(cmd)
        return current_apply(cmd)

    def acquire(request):
        for ch in request.channels:
            assert adapter.values[f"{ch}.channel"]["enabled"]
        assert request.initiate and request.confirmed
        requests.append(request)
        batch = current_acquire(request)
        if not overload:
            return batch
        return replace(
            batch, measurements=tuple(replace(m, quality=Quality.OVERLOAD) for m in batch.measurements)
        )

    monkeypatch.setattr(adapter, "apply", apply)
    monkeypatch.setattr(adapter, "acquire", acquire)
    window = RealWindow(runtime)
    qtbot.addWidget(window)
    window.show()
    try:
        window.navigate("实时监测", "oscilloscope", pico)
        scope = window.stack.currentWidget()
        panel = scope.controls
        assert isinstance(scope.plot, OscilloscopePlot)
        assert scope.plot.isVisible() and panel.isVisible() and scope.selector.isVisible()
        assert scope.single_action.isVisible()
        assert panel.channels() == (1,)
        assert panel.ranges[1].currentData() == "10" and panel.couplings[1].currentData() == "DC"
        assert panel.timebase.isVisible() and not panel.sample_interval.isVisible()
        panel.points.setValue(32)
        assert not applied and not requests
        assert scope.single_action.isEnabled()
        scope.single_action.trigger()

        def visible_frame():
            assert scope.plot.displayed_frame is not None, (
                ascii(scope.status.text()),
                ascii(panel.error),
                applied,
                requests,
            )

        qtbot.waitUntil(visible_frame)
        assert [(c.operation, c.channel, c.parameters) for c in applied] == [
            ("channel", 1, {"enabled": True, "range": "10", "coupling": "DC", "offset": 0})
        ]
        frame = scope.plot.displayed_frame
        assert not runtime.monitoring.snapshots().get(pico, {}).get("running")
        assert len(requests) == 1 and len(frame.batch.measurements) == 1
        assert scope.plot.display_specs[1].units_per_div == 2.5
        assert "CH1 −10.00 V" in scope.plot.bottom_voltage.text()
        panel.ranges[1].setCurrentIndex(panel.ranges[1].findData("1"))
        panel.offsets[1].setValue(0.1)
        assert scope.plot.display_specs[1].units_per_div == 2.5
        assert "CH1 −10.00 V" in scope.plot.bottom_voltage.text()
        panel.ranges[1].setCurrentIndex(panel.ranges[1].findData("10"))
        panel.offsets[1].setValue(0)
        measurement = frame.batch.measurements[0]
        assert (measurement.channel, measurement.unit, measurement.axis_unit) == (1, "V", "s")
        assert len(measurement.axis) == len(measurement.values) == 32
        assert np.all(np.isfinite(measurement.values))

        qtbot.waitUntil(scope.save_button.isEnabled)
        scope.save_button.click()
        qtbot.waitUntil(lambda: bool(list(frame_export_directory.rglob("*.csv"))))

        panel.checks[2].setChecked(True)
        panel.offsets[2].setValue(0.1)
        panel.timebase.setValue(12)
        scope.start_button.click()
        qtbot.waitUntil(lambda: len(requests) >= 4, timeout=8000)
        assert requests[-1].channels == (1, 2)
        assert [(c.operation, c.channel) for c in applied] == [
            ("channel", 1),
            ("channel", 2),
            ("timebase", 1),
        ]
        assert applied[1].parameters["offset"] == 0.1
        assert not panel.scroller.isEnabled()
        scope.start_button.click()
        qtbot.waitUntil(lambda: not runtime.monitoring.snapshots()[pico]["running"])
        qtbot.waitUntil(panel.scroller.isEnabled)

        scope.selector.select(scpi)
        assert scope.single_action.isVisible() and scope.splitter.isVisible()
        scope.selector.select(pico)
        panel = scope.controls
        assert panel.channels() == (1, 2) and panel.timebase.value() == 12 and panel.points.value() == 32
        assert panel.offsets[2].value() == 0.1
        panel.checks[1].setChecked(False)
        panel.capture_mode.setCurrentIndex(panel.capture_mode.findData("streaming"))
        panel.sample_interval.setValue(4)
        qtbot.waitUntil(lambda: panel.sample_interval.isVisible() and not panel.timebase.isVisible())
        assert len(applied) == 3
        scope.start_button.click()
        qtbot.waitUntil(
            lambda: (
                scope.plot.displayed_frame is not None
                and scope.plot.displayed_frame.request.kind == "streaming"
            )
        )
        assert requests[-1].kind == "streaming" and requests[-1].channels == (2,)
        assert set(scope.plot.display_specs) == {2}
        axis = scope.plot.displayed_frame.batch.measurements[0].axis
        assert np.isclose(scope.plot.grid_range, (axis[0], axis[-1])).all()
        assert scope.plot.display_specs[2].reference_value == -0.1
        assert [(c.operation, c.parameters) for c in applied[3:]] == [
            ("channel", {"enabled": False, "range": "10", "coupling": "DC", "offset": 0}),
            ("sample_interval", {"value": 4e-6}),
        ]
        scope.start_button.click()
        qtbot.waitUntil(panel.scroller.isEnabled)
        overload = True
        qtbot.waitUntil(scope.single_action.isEnabled)
        scope.single_action.trigger()

        def overload_status():
            assert "CH 2 输入过载" in scope.status.text(), (
                ascii(scope.status.text()),
                ascii(panel.error),
                len(requests),
                requests[-1].kind,
            )

        qtbot.waitUntil(overload_status)
        qtbot.waitUntil(panel.scroller.isEnabled)
        saved_frame = scope.plot.displayed_frame
        overload = False
        panel.ranges[2].setCurrentIndex(panel.ranges[2].findData("20"))
        scope.single_action.trigger()
        qtbot.waitUntil(
            lambda: scope.plot.displayed_frame is not saved_frame and scope.plot.displayed_frame is not None
        )
        assert applied[-1].parameters["range"] == "20"
        assert scope.plot.displayed_frame.request.kind == "streaming"
        assert scope.plot.displayed_frame.batch.performance["single_completed"]
        assert scope.plot.displayed_frame.batch.measurements[0].quality == Quality.SIMULATED
        qtbot.waitUntil(panel.scroller.isEnabled)
        panel.checks[2].setChecked(False)
        assert not scope.single_action.isEnabled() and not scope.start_button.isEnabled()
    finally:
        window.timer.stop()
        window.controller.close()
        runtime.close()
        window.closing = True
        window.bridge.worker.shutdown()
