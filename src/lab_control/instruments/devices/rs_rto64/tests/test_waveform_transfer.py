import json
from pathlib import Path

import numpy as np
import pytest

from lab_control.domain.connections import ConnectionConfig
from lab_control.domain.instruments import AcquisitionRequest, DeviceError, DeviceIdentity
from lab_control.instruments.devices.rs_rto64.adapter import Rto64Adapter, rto64
from lab_control.transport.base import MockTransport


def capture(exchanges):
    wire = MockTransport(exchanges)
    wire.open()
    adapter = Rto64Adapter(wire, rto64(), ConnectionConfig("mock://waveform", "rs.rto64"))
    adapter.identity = DeviceIdentity("Rohde&Schwarz", "RTO64", "TEST")
    result = adapter.acquire(AcquisitionRequest("waveform", allow_setup=False))
    assert not wire.exchanges and all(cmd.endswith("?") for cmd in wire.history)
    return result.measurements[0]


def binary_frame():
    case = json.loads((Path(__file__).parent / "fixtures/acquisition.json").read_text(encoding="utf8"))
    payload = np.array(
        [(-1.0, 1.25), (0.0, 3.25), (1.0, -2.5)], dtype=[("time", "<f8"), ("voltage", "<f4")]
    ).tobytes()
    data = b"#2" + str(len(payload)).encode() + payload + b"\n"
    replies = {"FORM?": b"REAL,32", "EXP:WAV:INCX?": b"1", "CHAN1:DATA?": data}
    return [(q, replies.get(q, v.encode())) for q, v in case["exchanges"]]


def test_passive_binary_xy():
    m = capture(binary_frame())
    np.testing.assert_array_equal(m.values, [1.25, 3.25, -2.5])
    np.testing.assert_array_equal(m.axis, [-1, 0, 1])
    assert (m.axis_unit, m.unit) == ("s", "V")


@pytest.mark.parametrize(
    ("query", "bad"),
    [
        ("PROB1:SET:ATT:UNIT?", b"A"),
        ("CHAN1:STAT?", b"0"),
        ("EXP:WAV:MULT?", b"1"),
        ("FORM?", b"INT,16"),
        ("CHAN1:DATA:HEAD?", b"0,1,3,2"),
        ("CHAN1:DATA?", b"#236short"),
    ],
)
def test_invalid_layout_or_truncated_block(query, bad):
    with pytest.raises(DeviceError):
        capture([(q, bad if q == query else v) for q, v in binary_frame()])
