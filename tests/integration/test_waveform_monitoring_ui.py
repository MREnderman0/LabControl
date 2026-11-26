from dataclasses import replace

import numpy as np
import pytest

from lab_control.application.config import ConnectionConfig
from lab_control.application.runtime import ApplicationRuntime
from lab_control.ui.real_window import RealWindow


def test_scope_page_and_overview_fit_current_frame_without_instrument_io(
    qtbot, tmp_path, frame_export_directory, monkeypatch
):
    runtime = ApplicationRuntime(root=tmp_path, hardware=False)
    key = runtime.add(ConnectionConfig("mock://scope-ui", "lecroy.9404m", "mock"))
    runtime.connect(key).result(3)
    adapter = runtime.sessions.sessions[key].adapter
    adapter.values["1.timebase"] = {"value": 1e-6}
    acquire = adapter.acquire
    calls = []

    def short_trace(request):
        calls.append(request)
        batch = acquire(request)
        return replace(
            batch,
            measurements=tuple(
                replace(
                    m,
                    axis=np.linspace(-5e-6, 5e-6, len(m.values)),
                    axis_unit="s",
                )
                for m in batch.measurements
            ),
        )

    adapter.acquire = short_trace
    window = RealWindow(runtime)
    qtbot.addWidget(window)
    window.show()
    window.navigate("实时监测", "oscilloscope", key)
    page = window.stack.currentWidget()
    try:
        qtbot.waitUntil(lambda: len(page.controls.channels()) == 4)
        page.start()
        qtbot.waitUntil(lambda: page.plot.displayed_frame is not None, timeout=5000)
        runtime.monitoring.stop(key).result(3)
        assert all(len(c.getData()[0]) > 1 for c in page.plot.curves.values())
        reads_after_stop = len(calls)
        page.select_device("")
        page.select_device(key)
        qtbot.waitUntil(lambda: page.plot.displayed_frame is not None, timeout=3000)
        assert len(calls) == reads_after_stop
        assert page.timing.minimum() == 0 and page.timing.maximum() == 60
        page.plot.plot.setXRange(-1, 1, padding=0)
        page.fit_button.click()
        assert page.plot.plot.viewRange()[0][1] < 1e-5
        frame = page.plot.displayed_frame
        with monkeypatch.context() as cancelled:
            cancelled.setattr("lab_control.ui.controller.choose_frame_destination", lambda _parent: None)
            cancelled.setattr(
                runtime.monitoring, "export_displayed_frame", lambda *a, **kw: pytest.fail("cancelled export")
            )
            page.save()
        assert not list(frame_export_directory.iterdir())
        page.save()
        qtbot.waitUntil(lambda: bool(list(frame_export_directory.rglob("samples.npz"))), timeout=5000)
        with np.load(next(frame_export_directory.rglob("samples.npz"))) as saved:
            np.testing.assert_array_equal(saved["axis_0"], frame.batch.measurements[0].axis)
            np.testing.assert_array_equal(saved["values_0"], frame.batch.measurements[0].values)
        assert len(calls) == reads_after_stop
    finally:
        window.timer.stop()
        window.controller.close()
        runtime.close()
        window.closing = True
        window.bridge.worker.shutdown()
