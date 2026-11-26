import time
from dataclasses import replace
from threading import RLock
from types import SimpleNamespace

from lab_control.application.monitoring_state import MonitorFrame
from lab_control.application.sessions import SettingsFrame
from lab_control.domain.instruments import (
    AcquisitionRequest,
    DeviceIdentity,
    Measurement,
    MeasurementBatch,
    ParameterReading,
)
from lab_control.ui.workspace.overview import WorkspaceOverview


def test_overview_uses_current_output_readback_and_labels_supply_setpoints():
    readings = {}
    state = {"sessionId": "live", "configurationVersion": 3}
    window = SimpleNamespace(
        runtime=SimpleNamespace(
            monitoring=SimpleNamespace(tasks={}, lock=RLock()),
            sessions=SimpleNamespace(readback_frames=lambda _: readings),
        ),
        controller=SimpleNamespace(states={"d": state}),
    )
    overview = WorkspaceOverview(window)

    def show(adapter):
        return overview._device_readout({"id": "d", "adapterId": adapter}, "connected", {}, [])[1]

    def reading(name, value):
        return SettingsFrame(
            "live", 3, 1, time.perf_counter(), ParameterReading(name, 1, value, "instrument")
        )

    readings[1, "frequency"] = reading("frequency", 1e6)
    for value, label in ((True, "输出开"), (False, "输出关")):
        readings[1, "output"] = reading("output", value)
        assert show("rs.sma100b") == f"CH1 · 1 MHz · {label}"
    readings[1, "output"] = replace(readings[1, "output"], configuration_version=2)
    assert "输出关" not in show("rs.sma100b")
    readings[1, "voltage"] = reading("voltage", 5)
    readings[1, "current"] = reading("current", 0.2)
    setpoints = "CH1 · 设定 5.000 V · 限流 0.200 A"
    assert show("keysight.e36313a") == setpoints
    batch = MeasurementBatch(
        DeviceIdentity("Keysight", "E36313A", "fixture"),
        (Measurement("voltage", "V", [4.7], 2), Measurement("current", "A", [0.3], 1)),
        {},
    )
    frame = MonitorFrame(1, 3, AcquisitionRequest("scalar", (1, 2)), batch, session_id="live")
    frames = {("scalar", ""): frame}
    window.runtime.monitoring.tasks["d"] = SimpleNamespace(frames=frames)
    assert show("keysight.e36313a") == "CH2 实测 4.700 V"
    frames["scalar", ""] = replace(frame, configuration_version=2)
    assert show("keysight.e36313a") == setpoints
