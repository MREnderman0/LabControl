from threading import Event

import pytest

from lab_control.domain.instruments import AcquisitionRequest, DeviceCommand, DeviceError
from lab_control.instruments.common.lecroy_ranges import cvar_query
from lab_control.instruments.scope_single import SingleCapture

from .test_adapter import scope


def scale_limits():
    return [
        (cvar_query("scale", 2, member), value)
        for member, value in (("GetMinValue", b"0.01"), ("GetMaxValue", b"1"), ("GetGrainValue", b"0.001"))
    ]


def test_uses_live_channel_limits_and_instrument_readback():
    adapter, wire = scope(
        scale_limits() + [("C2:VDIV 0.2", None), ("*OPC?", b"1"), ("C2:VDIV?", b"C2:VDIV 200 MV")]
    )
    result = adapter.apply(DeviceCommand("scale", {"value": 0.2}, 2))
    assert result.outcome == "success" and result.readback == 0.2
    assert not wire.exchanges


def test_bandwidth_write_uses_maui_enum_and_independent_channel_readback():
    adapter, wire = scope(
        [
            ("VBS 'app.Acquisition.C3.BandwidthLimit=\"20MHz\"'", None),
            ("*OPC?", b"*OPC 1"),
            ("VBS? 'return=app.Acquisition.C3.BandwidthLimit'", b"VBS 20MHz"),
        ]
    )
    result = adapter.apply(DeviceCommand("bandwidth_limit", {"value": "20MHz"}, 3))
    assert result.outcome == "success" and result.readback == "20MHZ" and not wire.exchanges


def test_range_change_rejects_before_writing():
    adapter, wire = scope(scale_limits())
    with pytest.raises(DeviceError):
        adapter.apply(DeviceCommand("scale", {"value": 2}, 2))
    assert all("?" in cmd for cmd in wire.history)


def test_trigger_readback_verifies_holdoff_and_refuses_advanced_trigger():
    old = b"TRSE EDGE,SR,C2,HT,TI,HV,1E-3S"
    new = b"TRSE EDGE,SR,C1,HT,OFF"
    adapter, wire = scope(
        [
            ("TRSE?", old),
            ("TRSE EDGE,SR,C1", None),
            ("*OPC?", b"1"),
            ("TRSE?", new),
            ("TRSE?", new),
            ("TRSE?", b"TRSE WIDTH,SR,C2,HT,OFF"),
        ]
    )
    result = adapter.apply(DeviceCommand("trigger_source", {"value": "C1"}))
    assert result.outcome == "rejected" and "1.trigger_source" not in adapter.configuration
    with pytest.raises(DeviceError):
        adapter.apply(DeviceCommand("trigger_slope", {"value": "NEG"}, 2))
    assert not wire.exchanges


def test_advanced_source_without_sr_remains_readable_and_blocks_only_edge_controls():
    adapter, wire = scope([("TRSE?", b"TRSE PATTERN"), ("TRSE?", b"TRSE PATTERN"), ("TDIV?", b"1 MS")])
    assert adapter.read("trigger_source") == "PATTERN"
    with pytest.raises(DeviceError):
        adapter.apply(DeviceCommand("trigger_level", {"value": 0.1}, 2))
    assert adapter.read("timebase") == 0.001 and not wire.exchanges


def test_single_discards_old_event_and_restores_normal_after_cancel():
    adapter, wire = scope(
        [
            ("TDIV?", b"1 MS"),
            ("C2:TRA?", b"ON"),
            ("VBS? 'return=app.Acquisition.C2.AverageSweeps'", b"1"),
            ("TRMD?", b"NORM"),
            ("TRMD STOP", None),
            ("TRMD?", b"STOP"),
            ("INR?", b"1"),
            ("TRMD SINGLE", None),
            ("INR?", b"0"),
            ("TRMD NORM", None),
            ("TRMD?", b"NORM"),
        ]
    )
    cancel = Event()
    job = SingleCapture(AcquisitionRequest("waveform", (2,), confirmed=True), cancel=cancel)

    def wait(complete, span, count):
        assert not complete()
        cancel.set()
        job.check()

    job.wait = wait
    with pytest.raises(DeviceError, match="取消"):
        job.capture(adapter, lambda _: pytest.fail("old trace was read"))
    assert not wire.exchanges and wire.history.count("TRMD SINGLE") == 1


def test_setting_single_does_not_rearm_an_existing_wait():
    adapter, wire = scope([("TRMD?", b"TRMD SINGLE")])
    with pytest.raises(DeviceError) as error:
        adapter.apply(DeviceCommand("trigger_mode", {"value": "single"}, confirmed=True))
    assert error.value.code == "task_busy" and wire.history == ["*IDN?", "TRMD?"]


def test_single_never_arms_a_disabled_channel():
    adapter, wire = scope([("TDIV?", b"1 MS"), ("C3:TRA?", b"OFF")])
    job = SingleCapture(AcquisitionRequest("waveform", (3,), confirmed=True))
    with pytest.raises(DeviceError, match="disabled"):
        job.capture(adapter, lambda _: pytest.fail("disabled channel was read"))
    assert not wire.exchanges


def test_single_accepts_event_only_after_new_arm_and_completion():
    adapter, wire = scope(
        [
            ("TRMD STOP", None),
            ("TRMD?", b"STOP"),
            ("INR?", b"1"),
            ("TRMD SINGLE", None),
            ("INR?", b"1"),
            ("TRMD?", b"SINGLE"),
            ("INR?", b"0"),
            ("TRMD?", b"STOP"),
        ]
    )
    completed = adapter.single_arm()
    assert not completed()
    assert completed()
    assert not wire.exchanges
