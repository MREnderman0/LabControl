import struct

import numpy as np
import pytest

from lab_control.domain.connections import ConnectionConfig
from lab_control.domain.instruments import AcquisitionRequest, DeviceCommand, DeviceError, DeviceIdentity
from lab_control.instruments.registry import build_scpi_adapter, match
from lab_control.transport.base import MockTransport

from ..profile import ADAPTER_ID


def frame(ch):
    desc = bytearray(346)
    desc[:8] = b"WAVEDESC"
    desc[16:26] = b"LECROY_2_3"
    for pos, fmt, value in (
        (34, "h", 1),
        (36, "i", 346),
        (60, "i", 4),
        (116, "i", 4),
        (124, "i", 1),
        (128, "i", 3),
        (144, "i", 1),
        (156, "f", 0.25),
        (160, "f", 1),
        (176, "f", 0.5),
        (180, "d", -1),
        (328, "f", 1),
        (336, "f", 1),
        (344, "h", ch - 1),
    ):
        struct.pack_into("<" + fmt, desc, pos, value)
    desc[196], desc[244] = ord("V"), ord("s")
    return f"C{ch}:WF ALL,#3350".encode() + desc + struct.pack("4b", 12, -8, 0, ch)


def snapshot(channels, *, enabled=b"ON", dt=b"1 MS"):
    return (
        [(f"C{ch}:TRA?", enabled) for ch in channels]
        + [(f"C{ch}:VDIV?", b"50 MV") for ch in channels]
        + [(f"C{ch}:OFST?", b"0 V") for ch in channels]
        + [
            ("TDIV?", dt),
            ("TRDL?", b"0 S"),
            ("TRMD?", b"AUTO"),
            ("CFMT?", b"DEF9,BYTE,BIN"),
            ("CORD?", b"LO"),
            ("WFSU?", b"SP,0,NP,0,FP,0,SN,0"),
        ]
    )


def scope(exchanges):
    wire = MockTransport([("*IDN?", b"*IDN LECROY,WAVERUNNER8254M,TEST,10.6.0"), *exchanges])
    wire.open()
    adapter = build_scpi_adapter(ConnectionConfig("mock://scope", ADAPTER_ID), wire)
    adapter.identify()
    return adapter, wire


def test_selected_channels_have_independent_si_samples_and_panel_metadata():
    channels = (4, 2)
    panel = snapshot(channels)
    adapter, wire = scope(panel + [(f"C{ch}:WF? ALL", frame(ch)) for ch in channels] + panel)
    batch = adapter.acquire(AcquisitionRequest("waveform", channels))
    assert batch.validation == "full" and [m.channel for m in batch.measurements] == [4, 2]
    for m in batch.measurements:
        np.testing.assert_array_equal(m.values, [-3, -1, m.channel * 0.25 - 1])
        np.testing.assert_array_equal(m.axis, [-0.5, 0, 0.5])
        assert (m.unit, m.axis_unit) == ("V", "s")
        assert m.metadata["scope_display"]["units_per_div"] == 0.05
    assert not wire.exchanges
    assert match(DeviceIdentity("LeCroy", "WaveRunner8254M-MS")) != ADAPTER_ID
    assert match(DeviceIdentity("LeCroy", "WaveRunner9404M")) != ADAPTER_ID


@pytest.mark.parametrize("failure", ["disabled", "wrong_source", "truncated", "changed", "non_voltage"])
def test_reject_invalid_frame_without_replaying_commands(failure):
    ch = (2,)
    panel = snapshot(ch, enabled=b"OFF" if failure == "disabled" else b"ON")
    payload = frame(3 if failure == "wrong_source" else 2)
    if failure == "truncated":
        payload = payload[:-1]
    if failure == "non_voltage":
        pos = payload.index(b"WAVEDESC") + 196
        payload = payload[:pos] + b"A" + payload[pos + 1 :]
    query = "C2:WF? ALL"
    adapter, wire = scope(panel + [(query, payload)] + snapshot(ch, dt=b"2 MS"))
    with pytest.raises(DeviceError) as error:
        adapter.acquire(AcquisitionRequest("waveform", ch))
    if failure == "changed":
        assert error.value.code == "configuration_changed"
    assert wire.history.count(query) == int(failure != "disabled")


def test_writes_and_initiation_rejected_before_io():
    adapter, wire = scope([])
    with pytest.raises(DeviceError):
        adapter.apply(DeviceCommand("scale", {"value": 0.2}))
    with pytest.raises(DeviceError):
        adapter.acquire(AcquisitionRequest("waveform", initiate=True, confirmed=True))
    assert wire.history == ["*IDN?"]
