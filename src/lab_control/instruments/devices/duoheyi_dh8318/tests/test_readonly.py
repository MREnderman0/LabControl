from dataclasses import replace

import pytest

from lab_control.domain.connections import ConnectionConfig
from lab_control.domain.instruments import AcquisitionRequest, DeviceCommand, DeviceError
from lab_control.instruments.devices.duoheyi_dh8318 import DEFINITION
from lab_control.instruments.devices.duoheyi_dh8318.adapter import CHANNELS, Dh8318Adapter, create_transport
from lab_control.transport.base import MockTransport

CONFIG = ConnectionConfig("COM7", "duoheyi.dh8318", "serial", baud_rate=115200)
IDN = b"DUOHEYI,DH-8318,TEST0001,VER:DEMO\n"


def connected(exchanges, selected=b"ALL\n"):
    io = MockTransport([("*IDN?", IDN), ("CH?", selected), *exchanges])
    io.open()
    adapter = Dh8318Adapter(io, DEFINITION.profile, CONFIG)
    assert adapter.identify().serial == "TEST0001"
    return adapter, io


def test_bulk_snapshot_uses_one_query_per_quantity_and_retains_channel_order():
    v = b"+10, 20,30,40,5.0E1,60,70,80\n"
    a = b"-1.0E-3,2,3,4,5,6,7,8\n"
    adapter, io = connected([("CH?", b"ALL\n"), ("MEAS:VOLT?", v), ("MEAS:CURR?", a), ("CH?", b"ALL\n")])
    result = adapter.acquire(AcquisitionRequest("scalar", (5, 1), points=1, allow_setup=False))
    assert [(m.channel, m.quantity, m.values[0], m.unit) for m in result.measurements] == [
        (5, "voltage", 50, "V"),
        (5, "current", 5, "A"),
        (1, "voltage", 10, "V"),
        (1, "current", -0.001, "A"),
    ]
    assert io.history == ["*IDN?", "CH?", "CH?", "MEAS:VOLT?", "MEAS:CURR?", "CH?"]


def test_single_selected_channel_is_not_relabeled_as_ch1():
    adapter, io = connected(
        [("CH?", b"6\n"), ("MEAS:CURR?", b"0\n"), ("CH?", b"6\n"), ("CH?", b"6\n")], selected=b"6\n"
    )
    assert adapter.selected_channels == (6,) and adapter.capabilities().channels == CHANNELS
    values = adapter.acquire(AcquisitionRequest("scalar", (6,), quantity="current")).measurements
    assert [(v.channel, v.quantity, v.values) for v in values] == [(6, "current", [0.0])]
    with pytest.raises(DeviceError) as failure:
        adapter.acquire(AcquisitionRequest("scalar", (1,), quantity="current"))
    assert failure.value.code == "configuration_changed"
    assert all(query.endswith("?") for query in io.history)


def test_channel_change_during_acquisition_discards_entire_batch():
    adapter, io = connected(
        [
            ("CH?", b"ALL\n"),
            ("MEAS:VOLT?", b"1,2,3,4,5,6,7,8\n"),
            ("MEAS:CURR?", b"0\n"),
            ("CH?", b"2\n"),
            ("CH?", b"2\n"),
            ("MEAS:VOLT?", b"19.1\n"),
            ("CH?", b"2\n"),
        ]
    )
    with pytest.raises(DeviceError) as failure:
        adapter.acquire(AcquisitionRequest("scalar", (1, 2)))
    assert failure.value.code == "configuration_changed"
    assert adapter.capability_details["acquisition_channels"] == (2,)
    assert adapter.read("measured_voltage", 2) == 19.1
    assert not io.exchanges


def test_single_to_all_change_before_request():
    adapter, _ = connected(
        [("CH?", b"ALL\n"), ("MEAS:VOLT?", b"1,2,3,4,5,6,7,8\n"), ("CH?", b"ALL\n")], selected=b"4\n"
    )
    result = adapter.acquire(AcquisitionRequest("scalar", (7,), quantity="voltage"))
    assert result.measurements[0].values == [7]
    assert adapter.capabilities().channels == CHANNELS


@pytest.mark.parametrize(
    "body",
    [
        b"1\n",
        b"1,2,3,4,5,6,7,8,",
        b"1,2,3,4,5,6,7,8,9\n",
        b"1,2,3,4,5,6,7,\n",
        b"1,2,3,4,5,6,7,NaN\n",
        b"1,2,3,4,5,6,7,Inf\n",
        b"1,2,3,4,5,6,7,1e999\n",
        b"1,2,3,4,5,6,7,8A\n",
        b"1,2,3,4,5,6,7,8",
        b"\xff\n",
        b"\n",
    ],
)
def test_bad_or_truncated_reply_is_never_a_scalar_frame(body):
    adapter, _ = connected([("CH?", b"ALL\n"), ("MEAS:CURR?", body), ("CH?", b"ALL\n")])
    with pytest.raises(DeviceError):
        adapter.acquire(AcquisitionRequest("scalar", (4,), quantity="current"))


@pytest.mark.parametrize("selection", [b"0\n", b"9\n", b"1,2\n", b"", b"ALL"])
def test_invalid_selection(selection):
    adapter, io = connected([("CH?", selection)])
    with pytest.raises(DeviceError):
        adapter.read_status()
    assert io.history[-1] == "CH?"


@pytest.mark.parametrize(
    "raw",
    [
        b"DUOHEYI,DH-8311,TEST0001,FW\n",
        b"OtherDUOHEYI,DH-8318,TEST0001,FW\n",
        b"DUOHEYI,DH-8318,,FW\n",
        IDN.rstrip(b"\n"),
    ],
)
def test_wrong_or_partial_identity_rejected_before_additional_queries(raw):
    io = MockTransport([("*IDN?", raw)])
    io.open()
    with pytest.raises(DeviceError):
        Dh8318Adapter(io, DEFINITION.profile, CONFIG).identify()
    assert io.history == ["*IDN?"]


def test_measurement_only_operations_and_active_capture_are_rejected():
    adapter, io = connected([])
    for operation in ("measured_voltage", "output", "short", "channel"):
        with pytest.raises(DeviceError):
            adapter.apply(DeviceCommand(operation, {"value": 1}, confirmed=True))
    with pytest.raises(DeviceError):
        adapter.acquire(AcquisitionRequest("scalar", initiate=True, confirmed=True))
    assert io.history == ["*IDN?", "CH?"]


def test_serial_transport_uses_configured_rate_and_lf():
    transport = create_transport(CONFIG, None)
    assert (transport.baud_rate, transport.terminator) == (115200, b"\n")
    with pytest.raises(DeviceError):
        create_transport(replace(CONFIG, terminator="\r"), None)
