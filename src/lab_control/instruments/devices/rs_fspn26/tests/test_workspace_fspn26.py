from pathlib import Path

import numpy as np

from lab_control.application.controls import data_sources
from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.connections import ConnectionConfig
from lab_control.instruments.devices.rs_fspn26 import DEFINITION
from lab_control.instruments.devices.rs_fspn26.adapter import Fspn26Adapter
from lab_control.instruments.devices.rs_fspn26.tests.test_fspn26 import TraceInstrument
from lab_control.ui.pages.device_control.phase_noise import PhaseNoisePage
from lab_control.ui.real_window import RealWindow


def test_displayed_trace_page_records_one_passive_frame(qtbot, tmp_path):
    transport = TraceInstrument()
    config = ConnectionConfig(transport.resource, DEFINITION.adapter_id, "mock")
    (source,) = data_sources(config)
    assert source.kind == "trace" and not source.request((1,)).allow_setup
    runtime = ApplicationRuntime(root=tmp_path, hardware=False)
    runtime.sessions.factory = lambda selected: Fspn26Adapter(transport, DEFINITION.profile, selected)
    key = runtime.add(config)
    runtime.connect(key).result(3)
    window = RealWindow(runtime)
    qtbot.addWidget(window)
    try:
        runtime.monitoring.enable_settings_sync(False)
        window.show()
        window.navigate("设备管理", "phase_noise_analyzer", key)
        page = window.stack.currentWidget()
        assert isinstance(page, PhaseNoisePage)
        qtbot.waitUntil(page.start_button.isEnabled)
        assert page.start_button.text() == "读取当前相噪曲线"
        assert not page.progress.isVisible() and not page.stability_plot.isVisible()
        assert not page.tabs.isTabVisible(page.tabs.indexOf(page.phase_plot))
        assert "TRAC3? TRACE1" not in transport.history
        page.start_button.click()
        qtbot.waitUntil(lambda: key in page.results, timeout=10000)
        assert set(page.results[key]) == {"phase_noise"}
        np.testing.assert_array_equal(page.results[key]["phase_noise"][0], [10, 42, 1000])
        assert len(list((Path(page.paths[key]) / "_raw").glob("segment-*.sqlite3"))) == 1
        assert transport.history.count("TRAC3? TRACE1") == 1
    finally:
        window.controller.close()
        window.closing = True
        window.bridge.worker.shutdown()
        runtime.close()
