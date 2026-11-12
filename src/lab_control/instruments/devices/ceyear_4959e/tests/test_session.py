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
    asset = ConnectionConfig("mock://ceyear-session", DEFINITION.adapter_id, "mock", expected_serial="TEST")
    replies = {
        "*IDN?": b"Ceyear Technologies,4959E,TEST,2.6.14",
        ":INST?": b"SA",
        ":SWE:MODE?": b"LINE",
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
        replies.update({":SWE:MODE?": b"Error!", ":DET:FUNC?": b"NORM"})
        replies[TRACE_QUERY] = b",".join([b"-90.25"] * 202)
        with pytest.raises(Unsupported, match="202.*101"):
            runtime.sessions.submit(device, "acquire", request).result(timeout=5)
    assert transport.history.count(TRACE_QUERY) == 2
    assert not list(tmp_path.rglob("*.csv"))
