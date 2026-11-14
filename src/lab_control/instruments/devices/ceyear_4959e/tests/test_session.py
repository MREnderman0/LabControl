import json

import numpy as np
import pytest

from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.connections import ConnectionConfig
from lab_control.domain.instruments import AcquisitionRequest, Quality, Unsupported
from lab_control.instruments import registry
from lab_control.instruments.devices.ceyear_4959e import DEFINITION
from lab_control.instruments.devices.ceyear_4959e.adapter import TRACE_QUERY
from lab_control.transport.base import MockTransport


def test_passive_session_returns_instrument_axis_and_saves_no_failed_frame(tmp_path, monkeypatch):
    asset = ConnectionConfig(
        "mock://ceyear-session",
        DEFINITION.adapter_id,
        "mock",
        expected_serial="TEST",
    )
    replies = {
        "*IDN?": b"Ceyear Technologies,4959E,TEST,2.6.14",
        ":INST?": b"SA",
        ":FREQ:CENT?": b"100000000",
        ":FREQ:SPAN?": b"2000000",
        ":FORM?": b"ASC",
        ":FREQ:STAR?": b"99000000",
        ":FREQ:STOP?": b"101000000",
        ":SWE:POIN?": b"101",
        ":TRAC1:TYPE?": b"CLRW",
        ":DET:FUNC?": b"SAMP",
        ":BAND?": b"1000",
        TRACE_QUERY: b",".join([b"-90.25"] * 101),
    }

    class ReadOnlyMock(MockTransport):
        def query(self, command):
            self.history.append(command)
            return replies[command]

    transport = ReadOnlyMock([])
    monkeypatch.setitem(registry.DEVICE_DEFINITIONS, DEFINITION.adapter_id, DEFINITION)
    with ApplicationRuntime((asset,), root=tmp_path, hardware=False) as runtime:
        runtime.sessions.factory = lambda config: registry.build_scpi_adapter(config, transport)
        device = runtime.add(asset)
        identity = runtime.connect(device, configure_acquisition=False).result(timeout=5)
        assert identity.serial == "TEST"
        request = AcquisitionRequest("spectrum", channels=(1,), initiate=False, allow_setup=False)
        good = runtime.sessions.submit(device, "acquire", request).result(timeout=5)
        trace = good.measurements[0]
        assert trace.quality == Quality.GOOD and trace.axis_unit == "Hz"
        np.testing.assert_allclose(trace.axis, np.linspace(99e6, 101e6, 101))
        replies[":DET:FUNC?"] = b"NORM"
        replies[TRACE_QUERY] = b",".join([b"-90.25"] * 202)
        batch = runtime.sessions.submit(device, "acquire", request).result(timeout=5)
        raw = batch.measurements[0]
        assert raw.axis_unit == "Hz" and raw.metadata["points"] == 202
        np.testing.assert_array_equal(raw.axis, np.repeat(np.linspace(99e6, 101e6, 101), 2))
        runtime.repository.export(batch)
        with np.load(next(tmp_path.rglob("samples.npz")), allow_pickle=False) as saved:
            np.testing.assert_array_equal(saved["values_0"], raw.values)
            np.testing.assert_array_equal(saved["axis_0"], raw.axis)
            item = json.loads(str(saved["metadata_json"]))["measurements"][0]
            assert (item["unit"], item["axis_unit"]) == ("dBm", "Hz")
            assert item["metadata"]["settings"]["sweep_points"] == 101
            assert item["metadata"]["settings"]["sweep_mode_source"] == "linear_default"
        replies[":TRAC1:TYPE?"] = b"BLAN"
        with pytest.raises(Unsupported, match="trace 1 must be visible"):
            runtime.sessions.submit(device, "acquire", request).result(timeout=5)
    assert all(cmd.endswith("?") for cmd in transport.history)
    assert ":SWE:MODE?" not in transport.history
    assert transport.history.count(TRACE_QUERY) == 2
    assert len(list(tmp_path.rglob("samples.npz"))) == 1
