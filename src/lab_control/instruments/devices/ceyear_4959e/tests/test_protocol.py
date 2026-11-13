import json
from pathlib import Path

import numpy as np
import pytest

from lab_control.domain.connections import ConnectionConfig
from lab_control.domain.instruments import AcquisitionRequest, DeviceCommand, DeviceError
from lab_control.instruments.devices.ceyear_4959e import DEFINITION
from lab_control.instruments.devices.ceyear_4959e.adapter import PROFILE, TRACE_QUERY, MicrowaveAnalyzer
from lab_control.instruments.devices.ceyear_4959e.hardware.readonly import EXTRA_QUERIES, capture_request
from lab_control.transport.base import MockTransport

IDN = b"Ceyear Technologies,4959E,TEST,2.6.14"
POINTS = 101
TRACE = b",".join(str(-100 + i / 10).encode() for i in range(POINTS)) + b"\n"


def snapshot(
    *, mode="SA", scan="LINE", fmt="ASC", start=1e6, stop=2e6, trace="CLRW", detector="SAMP", rbw=100
):
    exchanges = [(":INST?", mode.encode())]
    if mode != "SA":
        return exchanges
    fields = [
        (":SWE:MODE?", scan),
        (":FORM?", fmt),
        (":FREQ:STAR?", start),
        (":FREQ:STOP?", stop),
        (":SWE:POIN?", POINTS),
        (":TRAC1:TYPE?", trace),
        (":DET:FUNC?", detector),
        (":BAND?", rbw),
    ]
    if start == stop:
        fields.append((":SWE:TIME?", 0.125))
    return exchanges + [(cmd, str(value).encode()) for cmd, value in fields]


def device(exchanges, idn=IDN):
    transport = MockTransport([("*IDN?", idn), *exchanges])
    transport.open()
    adapter = MicrowaveAnalyzer(transport, PROFILE, ConnectionConfig("mock://4959e", PROFILE.adapter_id))
    adapter.identify()
    return adapter, transport


@pytest.mark.parametrize("stop,last,unit", [(2e6, 2e6, "Hz"), (1e6, 0.125, "s")])
def test_trace_coordinates_and_guarded_commands(stop, last, unit):
    settings = snapshot(stop=stop)
    adapter, transport = device([*settings, (TRACE_QUERY, TRACE), *settings])
    request = capture_request(adapter)
    assert request.channels == (1,) and not request.initiate and not request.allow_setup
    batch = adapter.acquire(request)
    trace = batch.measurements[0]
    assert trace.quantity == "spectrum" and trace.unit == "dBm" and trace.axis_unit == unit
    np.testing.assert_allclose(trace.values, -100 + np.arange(POINTS) / 10)
    assert (trace.axis[0], trace.axis[-1]) == (1e6 if unit == "Hz" else 0, last)
    assert batch.validation == "full" and batch.configuration["sweep_points"] == POINTS
    allowed = {"*IDN?", *EXTRA_QUERIES, *(spec.query for spec in PROFILE.commands.values())}
    assert set(transport.history) <= allowed
    assert all(command.endswith("?") for command in transport.history)


@pytest.mark.parametrize(
    "change",
    [
        {"mode": "VNA"},
        {"scan": "LIST"},
        {"fmt": "HEX"},
        {"trace": "BLAN"},
        {"trace": "UNKNOWN"},
        {"stop": 0},
        {"rbw": 0},
    ],
)
def test_unqualified_context_fails_before_trace(change):
    adapter, transport = device(snapshot(**change))
    with pytest.raises(DeviceError):
        adapter.acquire(AcquisitionRequest("spectrum"))
    assert TRACE_QUERY not in transport.history


@pytest.mark.parametrize(
    "payload",
    [
        b"0,1",
        TRACE.replace(b"-100.0", b"INF"),
        TRACE.replace(b"-100.0", b"BAD"),
        TRACE.replace(b"-100.0", b"9.91E37"),
        TRACE + b"junk",
    ],
)
def test_malformed_trace_does_not_produce_a_frame(payload):
    adapter, transport = device([*snapshot(), (TRACE_QUERY, payload)])
    with pytest.raises(DeviceError) as error:
        adapter.acquire(AcquisitionRequest("spectrum"))
    assert error.value.code == "parse_error" and transport.history.count(TRACE_QUERY) == 1


@pytest.mark.parametrize("second", [TRACE, b",".join([b"-73.14"] * POINTS)])
def test_normal_detector_preserves_double_length_in_wire_order(second):
    settings = snapshot(scan="Error!", detector="NORM")
    payload = TRACE.strip() + b"," + second
    adapter, transport = device([*settings, (TRACE_QUERY, payload), *settings])
    result = adapter.acquire(capture_request(adapter)).measurements[0]
    assert (result.unit, result.axis_unit, result.metadata["axis_kind"]) == (
        "dBm",
        "index",
        "transfer_sample_index",
    )
    assert result.metadata["points"] == 2 * POINTS and result.metadata["settings"]["sweep_points"] == POINTS
    np.testing.assert_array_equal(result.axis, np.arange(POINTS * 2))
    np.testing.assert_array_equal(result.values, [float(item) for item in payload.split(b",")])
    assert all(cmd.endswith("?") for cmd in transport.history)


def test_front_panel_26_6_ghz_raw_trace_is_readable_without_extending_write_range():
    settings = [
        (q, b"1001" if q == ":SWE:POIN?" else value)
        for q, value in snapshot(scan="Error!", start=0, stop=26.6e9, detector="NORM", rbw=3e6)
    ]
    payload = b",".join([b"-89.96", b"-51.12"] * 1001)
    adapter, transport = device([*settings, (TRACE_QUERY, payload), *settings])
    result = adapter.acquire(capture_request(adapter)).measurements[0]
    assert result.axis_unit == "index" and result.metadata["settings"]["stop_frequency"] == 26.6e9
    np.testing.assert_array_equal(result.axis, np.arange(2002))
    np.testing.assert_array_equal(result.values, [-89.96, -51.12] * 1001)
    assert PROFILE.commands["stop_frequency"].capability.parameters["value"].maximum == 26.5e9
    assert all(cmd.endswith("?") for cmd in transport.history)


@pytest.mark.parametrize("length", [POINTS * 2 - 1, POINTS * 2 + 1])
def test_normal_detector_rejects_incomplete_or_extra_pairs(length):
    settings = snapshot(scan="Error!", detector="NORM")
    adapter, _ = device([*settings, (TRACE_QUERY, b",".join([b"-80"] * length))])
    with pytest.raises(DeviceError) as error:
        adapter.acquire(capture_request(adapter))
    assert error.value.code == "parse_error"


def test_unreported_sweep_mode_uses_only_indices_for_a_single_value_detector():
    settings = snapshot(scan="Error!", detector="SAMP")
    adapter, _ = device([*settings, (TRACE_QUERY, TRACE), *settings])
    trace = adapter.acquire(capture_request(adapter)).measurements[0]
    assert trace.axis_unit == "index"
    np.testing.assert_array_equal(trace.axis, np.arange(POINTS))


@pytest.mark.parametrize("after", [{"stop": 1.5e6}, {"mode": "VNA"}, {"trace": "VIEW"}])
def test_mid_trace_configuration_change_discards_frame(after):
    adapter, _ = device([*snapshot(), (TRACE_QUERY, TRACE), *snapshot(**after)])
    with pytest.raises(DeviceError) as error:
        adapter.acquire(AcquisitionRequest("spectrum"))
    assert error.value.code == "configuration_changed"


def test_timeout_and_foreign_models_are_not_retried():
    adapter, transport = device([*snapshot(), (TRACE_QUERY, DeviceError("timeout", "timeout"))])
    with pytest.raises(DeviceError) as error:
        adapter.acquire(AcquisitionRequest("spectrum"))
    assert error.value.code == "timeout" and transport.history.count(TRACE_QUERY) == 1
    for reply in (b"Other,4959E,TEST,2.6.14", b"Ceyear Technologies,4959EK,TEST,2.6.14"):
        with pytest.raises(DeviceError) as ident:
            device([], idn=reply)
        assert ident.value.code == "identity_conflict"


def test_limits_and_mode_checked_before_any_control():
    adapter, transport = device([(":INST?", b"SA"), (":FREQ:STOP?", b"1000")])
    with pytest.raises(DeviceError):
        adapter.apply(DeviceCommand("start_frequency", {"value": 1001}))
    with pytest.raises(DeviceError):
        adapter.acquire(AcquisitionRequest("spectrum", channels=(2,)))
    with pytest.raises(DeviceError):
        adapter.acquire(AcquisitionRequest("spectrum", initiate=True, confirmed=True))
    assert all(command.endswith("?") for command in transport.history)


def test_control_fixture_through_package_adapter():
    fixture = json.loads((Path(__file__).parent / "fixtures/control.json").read_text(encoding="utf-8"))
    adapter, _ = device([(cmd, reply.encode()) for cmd, reply in fixture["exchanges"]])
    result = adapter.apply(DeviceCommand(fixture["operation"], fixture["parameters"]))
    assert result.outcome == "success" and result.readback == 5
    assert DEFINITION.adapter_type is type(adapter)
