import csv
from pathlib import Path

import numpy as np

from lab_control.application.controls import data_sources
from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.connections import ConnectionConfig
from lab_control.instruments.devices.rs_fspn26 import DEFINITION
from lab_control.instruments.devices.rs_fspn26.adapter import Fspn26Adapter
from lab_control.instruments.devices.rs_fspn26.tests.test_round import NativeInstrument
from lab_control.ui.pages.device_control.phase_noise import PhaseNoisePage
from lab_control.ui.real_window import RealWindow


def test_complete_management_workflow(qtbot, tmp_path):
    transport = NativeInstrument()
    config = ConnectionConfig(transport.resource, DEFINITION.adapter_id, "mock")
    (source,) = data_sources(config)
    assert source.kind == "trace" and not source.request((1,)).allow_setup
    runtime = ApplicationRuntime(root=tmp_path, hardware=False)
    runtime.sessions.factory = lambda c: Fspn26Adapter(transport, DEFINITION.profile, c)
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
        native = page.fspn
        qtbot.waitUntil(lambda: native.single.isEnabled() and bool(native.baseline))
        assert native.pn.isVisible() and native.adev.isVisible() and not page.legacy_body.isVisible()
        native.preview.click()
        qtbot.waitUntil(lambda: key in native.last and not native.busy, timeout=20000)
        assert transport.rounds == 0
        native.single.click()
        qtbot.waitUntil(lambda: not native.busy, timeout=20000)
        frame, folder = native.last[key]
        assert frame["configuration"]["origin"] == "single_sweep" and transport.rounds == 1
        with (folder / "phase_noise.csv").open(encoding="utf8") as f:
            points = list(csv.DictReader(f))
        np.testing.assert_array_equal([float(p["offset_hz"]) for p in points], [10, 42, 1000, 1, 1100])
        np.testing.assert_allclose(
            [float(p["phase_noise_dbc_hz"]) for p in points], [-103.1, -115.3, -128.5, -54.2, -133.5]
        )
        with (folder / "allan_deviation.csv").open(encoding="utf8") as f:
            assert list(csv.reader(f)) == [["tau_s", "adev"], ["1e-05", "1.2e-08"], ["1.0", "1.3e-10"]]
        assert native.spots.rowCount() == 3 and native.spots.columnCount() == 3
        native.cont.click()
        qtbot.waitUntil(lambda: native.rounds >= 2, timeout=30000)
        native.stop.click()
        qtbot.waitUntil(lambda: not native.busy, timeout=20000)
        saved = list(Path(tmp_path).rglob("phase_noise.csv"))
        assert len(saved) == 2 + native.rounds
        previous = native.last[key]
        transport.replies["STAT:QUES:PNO:COND?"] = b"2"
        native.single.click()
        qtbot.waitUntil(lambda: "未找到" in native.status.text(), timeout=20000)
        assert native.last[key] is previous
        assert len(saved) == len(list(Path(tmp_path).rglob("phase_noise.csv")))
    finally:
        window.controller.close()
        window.closing = True
        window.bridge.worker.shutdown()
        runtime.close()
