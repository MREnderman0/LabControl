import pytest

from lab_control.domain.connections import ConnectionConfig
from lab_control.domain.instruments import AcquisitionRequest, DeviceCommand, DeviceError
from lab_control.instruments.common.readonly_hardware import record_trace
from lab_control.instruments.devices.keysight_e36313a.adapter import numeric, on_off
from lab_control.instruments.registry import build_scpi_adapter
from lab_control.transport.base import MockTransport


def instrument(rows):
    transport = MockTransport([("*IDN?", b"Keysight Technologies,E36313A,TEST,1"), *rows])
    transport.open()
    adapter = build_scpi_adapter(ConnectionConfig("mock://fixture", "keysight.e36313a"), transport)
    adapter.identify()
    return adapter, transport


def test_acquire_all_channels_without_select_or_settings(tmp_path):
    rows = [
        (f"MEAS:{name}? (@{ch})", str(value).encode())
        for ch in (1, 2, 3)
        for name, value in (("VOLT", ch), ("CURR", ch * 0.25))
    ]
    adapter, wire = instrument(rows)
    batch = adapter.acquire(AcquisitionRequest("scalar", channels=(1, 2, 3)))
    assert [(m.channel, m.unit, m.values[0]) for m in batch.measurements] == [
        (ch, unit, value) for ch in (1, 2, 3) for unit, value in (("V", ch), ("A", ch * 0.25))
    ]
    stored = record_trace(batch, tmp_path)
    assert [(s["channel"], s["unit"], s["minimum"]) for s in stored] == [
        (m.channel, m.unit, m.values[0]) for m in batch.measurements
    ]
    assert all(s["points"] == 1 and s["x_first"] is None and s["x_last"] is None for s in stored)
    assert wire.history == ["*IDN?", *[q for q, _ in rows]]


@pytest.mark.parametrize(
    ("channel", "operation", "value"), [(1, "voltage", 6.1), (2, "current", 2.1), (3, "voltage", 26)]
)
def test_limits_precede_io(channel, operation, value):
    adapter, wire = instrument([])
    with pytest.raises(DeviceError):
        adapter.apply(DeviceCommand(operation, {"value": value}, channel, True))
    assert wire.history == ["*IDN?"]


def test_paired_channel_rejects_independent_write():
    adapter, wire = instrument([("OUTP:PAIR?", b"SER")])
    with pytest.raises(DeviceError):
        adapter.apply(DeviceCommand("voltage", {"value": 3}, 2, True))
    assert wire.history == ["*IDN?", "OUTP:PAIR?"]


def test_synchronized_output_rejected():
    adapter, wire = instrument([("OUTP:COUP:CHAN?", b"ALL")])
    with pytest.raises(DeviceError):
        adapter.apply(DeviceCommand("output", {"value": True}, 1, True))
    assert wire.history == ["*IDN?", "OUTP:COUP:CHAN?"]


@pytest.mark.parametrize("raw", [b"", b"1,2", b"9.91E37", b"nan"])
def test_invalid_numeric(raw):
    with pytest.raises(DeviceError):
        numeric(raw)


def test_invalid_output_flag():
    with pytest.raises(DeviceError):
        on_off(b"2")


@pytest.mark.parametrize(
    ("selection", "channel", "allowed"),
    [
        (b"P6V", 1, True),
        (b"P6V", 2, True),
        (b"P6V,P25V", 3, True),
        (b"P6V,P25V", 2, False),
        (b"CH2,CH3", 2, False),
        (b"BAD", 1, False),
    ],
)
def test_output_only_switches_independent_channel(selection, channel, allowed):
    rows = [("OUTP:PAIR?", b"OFF"), ("OUTP:TRAC?", b"0")] if channel in (2, 3) else []
    rows.append(("OUTP:COUP:CHAN?", selection))
    if allowed:
        rows.extend(
            [(f"OUTP ON,(@{channel})", b""), (f"OUTP? (@{channel})", b"1"), ("SYST:ERR?", b'0,"No error"')]
        )
    adapter, wire = instrument(rows)
    command = DeviceCommand("output", {"value": True}, channel, True)
    if allowed:
        result = adapter.apply(command)
        assert result.readback is True
    else:
        with pytest.raises(DeviceError):
            adapter.apply(command)
    assert wire.history == ["*IDN?", *[q for q, _ in rows]]
