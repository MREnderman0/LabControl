from dataclasses import replace

import pytest

from lab_control.domain.connections import ConnectionConfig
from lab_control.domain.instruments import AcquisitionRequest, DeviceError
from lab_control.instruments.registry import build_scpi_adapter

from ..adapter import QUERIES
from ..protocol import channel_list, tagged_reading


class Meter:
    """A two-slot, initially idle meter; the active relay determines its returned tag."""

    resource = "mock://2701"

    def __init__(self):
        self.state = dict(
            cards="7708,7708",
            scan_list="(@101,201)",
            scan_state="NONE",
            trigger_source="IMM",
            continuous="1",
            front_inputs="0",
            monitor_state="0",
            buffer_feed="NEV",
            data_format="ASC",
            elements="READ,UNIT,RNUM,TST",
            system_channel="(@)",
            closed_relays="(@)",
        )
        self.writes = []
        self.reply = None
        self.serial = "123456"
        self.fail_restore = False

    def write(self, cmd):
        self.writes.append(cmd)
        if cmd == "ROUT:OPEN:ALL":
            if self.fail_restore:
                raise DeviceError("relay timeout", "timeout")
            self.state.update(system_channel="(@)", closed_relays="(@)")
        elif cmd.startswith("ROUT:CLOS "):
            self.state.update(system_channel=cmd.split()[1], closed_relays=cmd.split()[1])
        elif cmd.startswith("FORM:ELEM "):
            self.state["elements"] = cmd.split()[1]
        else:
            pytest.fail(cmd)

    def query(self, cmd):
        if cmd == "*IDN?":
            return b"KEITHLEY INSTRUMENTS INC.,MODEL 2701,TEST,A00"
        if cmd.startswith("SYST:CARD"):
            return self.serial.encode()
        if cmd.startswith("FUNC?"):
            return b'"VOLT:DC","VOLT:DC"' if "," in cmd else b'"VOLT:DC"'
        if cmd == "DATA:FRESH?":
            ch = channel_list(self.state["system_channel"])[0]
            if ch == 201 and self.reply is not None:
                if isinstance(self.reply, DeviceError):
                    raise self.reply
                return self.reply
            return f"+2.31450000E-01VDC,{ch}INTCHAN".encode()
        key = next(name for name, q in QUERIES.items() if q == cmd)
        return self.state[key].encode()


@pytest.fixture
def meter():
    transport = Meter()
    adapter = build_scpi_adapter(ConnectionConfig(transport.resource, "keithley.2701", "mock"), transport)
    adapter.identify()
    return adapter, transport


REQUEST = AcquisitionRequest(channels=(101, 201), confirmed=True)


def test_dynamic_scan_and_complete_tagged_frame(meter):
    adapter, transport = meter
    original = dict(transport.state)
    adapter.refresh_capabilities()
    assert adapter.capability_details["acquisition_channels"] == [101, 201]
    assert adapter.read_status().values["1.scan_list"] == "(@101,201)"
    assert adapter.capabilities().channels == (101, 201)
    with pytest.raises(DeviceError):
        adapter.acquire(replace(REQUEST, channels=(1,)))
    batch = adapter.acquire(REQUEST)
    assert [(m.channel, m.unit, m.values) for m in batch.measurements] == [
        (101, "V", (0.23145,)),
        (201, "V", (0.23145,)),
    ]
    assert transport.state == original
    assert adapter.acquire(replace(REQUEST, channels=(201,))).measurements[0].channel == 201


@pytest.mark.parametrize(
    "failure",
    [
        b"0VDC,101INTCHAN",
        b"9.9E37VDC,201INTCHAN",
        b"3C,201INTCHAN",
        b"0VDC",
        DeviceError("timeout", "timeout"),
    ],
)
def test_partial_frame_is_discarded_and_setup_restored(meter, failure):
    adapter, transport = meter
    original = dict(transport.state)
    transport.reply = failure
    with pytest.raises(DeviceError):
        adapter.acquire(REQUEST)
    assert transport.state == original
    assert transport.writes.count("ROUT:CLOS (@201)") == 1


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("scan_state", "INT"),
        ("buffer_feed", "ALW"),
        ("closed_relays", "(@143)"),
        ("front_inputs", "1"),
        ("data_format", "REAL,32"),
        ("scan_list", "(@143)"),
        ("cards", "NONE,7708"),
    ],
)
def test_incompatible_state_never_changes_a_setting(meter, key, value):
    adapter, transport = meter
    transport.state[key] = value
    with pytest.raises(DeviceError):
        adapter.acquire(REQUEST)
    assert not transport.writes


def test_passive_request_and_pseudocard_never_route(meter):
    adapter, transport = meter
    for request in (AcquisitionRequest(), replace(REQUEST, allow_setup=False)):
        with pytest.raises(DeviceError):
            adapter.acquire(request)
    transport.serial = "????????"
    with pytest.raises(DeviceError):
        adapter.refresh_capabilities()
    assert not transport.writes


def test_restore_failure_still_restores_format_and_blocks_next_frame(meter):
    adapter, transport = meter
    transport.fail_restore = True
    with pytest.raises(DeviceError, match="恢复"):
        adapter.acquire(REQUEST)
    assert transport.state["elements"] == "READ,UNIT,RNUM,TST"
    writes = list(transport.writes)
    with pytest.raises(DeviceError):
        adapter.acquire(REQUEST)
    assert transport.writes == writes


def test_channel_ranges_and_resistance_unit():
    assert channel_list("(@101:140,201:240)") == (*range(101, 141), *range(201, 241))
    with pytest.raises(DeviceError):
        channel_list("(@101:201)")
    sample = tagged_reading("1.00E+02OHM4W,204INTCHAN", 204, "FRES")
    assert (sample.quantity, sample.unit, sample.values) == ("resistance", "ohm", (100.0,))
