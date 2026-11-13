import numpy as np
import pytest

from lab_control.domain.connections import ConnectionConfig
from lab_control.domain.instruments import AcquisitionRequest, DeviceError
from lab_control.instruments.devices.rs_fspn26 import DEFINITION
from lab_control.instruments.devices.rs_fspn26.adapter import Fspn26Adapter, window_indices


class TraceInstrument:
    """Document-derived responses with a synthetic PN measurement."""

    resource = "mock://fspn"

    def __init__(self):
        self.replies = {
            **{f"DISP:WIND3:TRAC{t}:STAT?": b"0" for t in range(2, 7)},
            "*IDN?": b"Rohde&Schwarz,FSPN-26,TEST,FIRMWARE",
            "INST:LIST?": b"'PNO','Phase Noise'",
            "LAY:CAT?": b"'Adev',1,'Combined',3",
            "LAY:WIND1:TYPE?": b"ADEV",
            "LAY:WIND3:TYPE?": b"PNO",
            "DISP:WIND3:TRAC1:STAT?": b"1",
            "DISP:WIND3:TRAC1:RES?": b"PN",
            "TRAC3:POIN? TRACE1": b"3",
            "CONF:PNO:MEAS?": b"PNO",
            "FORM?": b"ASC,0",
            "STAT:QUES:PNO:COND?": b"0",
            "STAT:QUES:FREQ:COND?": b"0",
            "TRAC3? TRACE1": b"10,-103.1,42,-115.3,1000,-128.5",
        }
        self.history = []

    def query(self, command):
        self.history.append(command)
        return self.replies[command]

    def close(self):
        pass


def adapter(transport):
    instrument = Fspn26Adapter(
        transport, DEFINITION.profile, ConnectionConfig(transport.resource, DEFINITION.adapter_id)
    )
    instrument.identify()
    return instrument


def test_native_curve_uses_window_and_trace_units():
    transport = TraceInstrument()
    instrument = adapter(transport)
    batch = instrument.acquire(AcquisitionRequest("trace", allow_setup=False))
    (curve,) = batch.measurements
    assert (curve.quantity, curve.axis_unit, curve.unit) == ("phase_noise", "Hz", "dBc/Hz")
    np.testing.assert_array_equal(curve.axis, [10, 42, 1000])
    np.testing.assert_allclose(curve.values, [-103.1, -115.3, -128.5])
    assert transport.history.count("CONF:PNO:MEAS?") == 2
    assert transport.history.count("TRAC3? TRACE1") == 1
    assert all("?" in cmd for cmd in transport.history)


@pytest.mark.parametrize(
    ("command", "reply", "code"),
    [
        ("TRAC3:POIN? TRACE1", b"0", "waveform_unavailable"),
        ("CONF:PNO:MEAS?", b"BASE", "acquisition_setup_blocked"),
        ("FORM?", b"REAL,32", "acquisition_setup_blocked"),
        ("TRAC3? TRACE1", b"10,-101,10,-102,11,-103", "waveform_unavailable"),
        ("TRAC3? TRACE1", b"10,-101,100,-102,110,9.91e37", "waveform_unavailable"),
        ("TRAC3? TRACE1", b"10,-101,11", "parse_error"),
    ],
)
def test_invalid_trace_not_delivered(command, reply, code):
    transport = TraceInstrument()
    transport.replies[command] = reply
    with pytest.raises(DeviceError) as exc:
        adapter(transport).acquire(AcquisitionRequest("trace"))
    assert exc.value.code == code
    if command != "TRAC3? TRACE1":
        assert "TRAC3? TRACE1" not in transport.history
    if command == "TRAC3:POIN? TRACE1":
        assert "窗口 3 迹线 1" in str(exc.value)


def test_window_change_discards_trace():
    transport = TraceInstrument()
    query = transport.query

    def queried(command):
        value = query(command)
        if command == "TRAC3? TRACE1":
            transport.replies["TRAC3:POIN? TRACE1"] = b"4"
        return value

    transport.query = queried
    with pytest.raises(DeviceError) as exc:
        adapter(transport).acquire(AcquisitionRequest("trace"))
    assert exc.value.code == "configuration_changed"
    assert window_indices(b"'My, window',3,'Other',1") == (3, 1)


@pytest.mark.parametrize(("first_type", "points"), [(b"AM", b"3"), (b"PN", b"0")])
def test_ineligible_first_trace_is_skipped(first_type, points):
    transport = TraceInstrument()
    transport.replies.update(
        {
            "DISP:WIND3:TRAC1:RES?": first_type,
            "TRAC3:POIN? TRACE1": points,
            "DISP:WIND3:TRAC2:STAT?": b"1",
            "DISP:WIND3:TRAC2:RES?": b"PN",
            "TRAC3:POIN? TRACE2": b"3",
            "TRAC3? TRACE2": transport.replies["TRAC3? TRACE1"],
        }
    )
    batch = adapter(transport).acquire(AcquisitionRequest("trace"))
    assert batch.measurements[0].metadata["trace"] == 2
    assert "TRAC3? TRACE1" not in transport.history


@pytest.mark.parametrize(("phase", "reference"), [(2, 0), (0, 256), (2, 256), (0, 0)])
def test_empty_curve_reports_current_conditions(phase, reference):
    transport = TraceInstrument()
    transport.replies.update(
        {
            "TRAC3:POIN? TRACE1": b"0",
            "STAT:QUES:PNO:COND?": str(phase).encode(),
            "STAT:QUES:FREQ:COND?": str(reference).encode(),
        }
    )
    with pytest.raises(DeviceError) as exc:
        adapter(transport).acquire(AcquisitionRequest("trace"))
    assert ("未找到载波" in str(exc.value)) == bool(phase)
    assert ("外部参考异常" in str(exc.value)) == bool(reference)
    assert "窗口 3 迹线 1" in str(exc.value)
    assert all("?" in command for command in transport.history)
