from threading import Event

import numpy as np
import pytest

from lab_control.domain.instruments import AcquisitionRequest, DeviceCommand, DeviceError
from lab_control.instruments.devices.rs_fspn26.tests.test_fspn26 import TraceInstrument, adapter


class NativeInstrument(TraceInstrument):
    """Transcript with two different PN grids, a native deviation plot and custom spots."""

    def __init__(self):
        super().__init__()
        self.rounds = 0
        self.done = True
        self.stop = None
        self.replies.update(
            {
                "SENS:FREQ:STAR?": b"1",
                "SENS:FREQ:STOP?": b"1000000",
                "SENS:SWE:XFAC?": b"10",
                "INP:ATT:AUTO?": b"0",
                "INP:ATT?": b"5",
                "SENS:SWE:MODE?": b"NORM",
                "SENS:LIST:BWID:RAT?": b"10",
                "SENS:SWE:TIME?": b"22",
                "SENS:FREQ:CENT?": b"1e8",
                "SENS:POW:RLEV?": b"-4",
                "SYST:ERR?": b'0,"No error"',
                "*OPC?": b"1",
                "TRAC1:POIN? TRACE1": b"2",
                "TRAC1? TRACE1": b"1e-5,1.2e-8,1,1.3e-10",
                "DISP:WIND3:TRAC1:MODE?": b"WRIT",
                "DISP:WIND3:TRAC2:STAT?": b"1",
                "DISP:WIND3:TRAC2:MODE?": b"WRIT",
                "DISP:WIND3:TRAC2:RES?": b"PN",
                "TRAC3:POIN? TRACE2": b"2",
                "TRAC3? TRACE2": b"1,-54.2,1100,-133.5",
                "CALC3:SNO:DEC?": b"1",
                "CALC3:SNO:USER?": b"1",
            }
        )
        for trace in (1, 2):
            self.replies[f"CALC3:SNO:TRAC{trace}:DEC:X?"] = b"1,10"
            self.replies[f"CALC3:SNO:TRAC{trace}:DEC:Y?"] = b"-55,-88"
            self.replies[f"CALC3:SNO2:TRAC{trace}:Y?"] = b"-82.5"
        for i in range(1, 7):
            self.replies[f"CALC3:SNO{i}:STAT?"] = b"1" if i == 2 else b"0"
        self.replies["CALC3:SNO2:X?"] = b"1005.12345"
        self.esr = 0

    def query(self, command):
        if command == "*ESR?":
            self.history.append(command)
            result, self.esr = self.esr, 0
            if self.stop and self.rounds:
                self.stop.set()
            return str(result).encode()
        if command.startswith("LAY:ADD? "):
            self.history.append(command)
            assert command == "LAY:ADD? 'Combined',BEL,ADEV"
            self.replies["LAY:CAT?"] = b"'Combined',3,'new',1"
            return b"'new'"
        return super().query(command)

    def write(self, command):
        self.history.append(command)
        if command == "INIT:IMM;*OPC":
            self.rounds += 1
            self.esr = int(self.done)
        elif command.startswith("SENS:") or command.startswith("INP:"):
            path, value = command.split()
            self.replies[path + "?"] = value.encode()


def request(active=True):
    return AcquisitionRequest(
        "phase_noise_round" if active else "phase_noise_display",
        initiate=active,
        confirmed=active,
        allow_setup=active,
    )


def test_round_creates_only_missing_window_and_reads_native_xy():
    t = NativeInstrument()
    t.replies["LAY:CAT?"] = b"'Combined',3"
    (result,) = adapter(t).run_acquisition(request(), Event())
    assert [c.quantity for c in result.measurements] == ["phase_noise", "phase_noise", "adev"]
    np.testing.assert_array_equal(result.measurements[1].axis, [1, 1100])
    assert result.configuration["spot_noise"][2]["offset_hz"] == 1005.12345
    assert t.rounds == 1


def test_stop_aborts_exactly_once_and_never_reads_old_curves():
    t = NativeInstrument()
    t.done = False
    t.stop = Event()
    assert not list(adapter(t).run_acquisition(request(), t.stop))
    assert t.history.count("ABOR") == 1
    assert "TRAC3? TRACE1" not in t.history


def test_no_carrier_discards_cached_curves_but_passive_can_display_them():
    t = NativeInstrument()
    t.replies["STAT:QUES:PNO:COND?"] = b"2"
    a = adapter(t)
    with pytest.raises(DeviceError, match="未找到"):
        list(a.run_acquisition(request(), Event()))
    size = len(t.history)
    (batch,) = a.run_acquisition(request(False), Event())
    assert batch.configuration["settings"]["carrier_hz"] is None
    assert all("?" in cmd for cmd in t.history[size:])


def test_apply_only_changed_settings_and_readback():
    t = NativeInstrument()
    a = adapter(t)
    result = a.apply(DeviceCommand("phase_noise_settings", {"xcorr": 20, "rbw_percent": 10}))
    assert result.outcome == "success" and result.readback["xcorr"] == 20
    assert [cmd for cmd in t.history if "?" not in cmd] == ["SENS:SWE:XFAC 20"]
    t.replies["INP:ATT:AUTO?"] = b"1"
    with pytest.raises(DeviceError, match="自动衰减"):
        a.apply(DeviceCommand("phase_noise_settings", {"attenuation_db": 10}))


def test_switch_to_manual_and_restore_auto_without_extra_writes():
    t = NativeInstrument()
    t.replies["INP:ATT:AUTO?"] = b"1"
    a = adapter(t)
    result = a.apply(DeviceCommand("phase_noise_settings", {"attenuation_auto": False, "attenuation_db": 10}))
    assert result.outcome == "success"
    assert result.readback["attenuation_auto"] is False and result.readback["attenuation_db"] == 10
    assert a.apply(DeviceCommand("phase_noise_settings", {"attenuation_auto": True})).outcome == "success"
    assert [cmd for cmd in t.history if "?" not in cmd] == ["INP:ATT:AUTO 0", "INP:ATT 10", "INP:ATT:AUTO 1"]


@pytest.mark.parametrize(("reply", "message"), [(b"1e-5,1e-8,1", "点数"), (b"1e-5,1e-8,1,-1", "无效")])
def test_invalid_adev_rejects_whole_round(reply, message):
    t = NativeInstrument()
    t.replies["TRAC1? TRACE1"] = reply
    with pytest.raises(DeviceError, match=message):
        list(adapter(t).run_acquisition(request(), Event()))


def test_failed_second_export_never_publishes_half_a_pair(tmp_path):
    from lab_control.instruments.devices.rs_fspn26.export import export_round

    frame = {
        "sequence": 1,
        "measurements": [
            {"quantity": "phase_noise", "axis": [1], "values": [-65], "metadata": {"window": 3, "trace": 2}},
            {"quantity": "adev", "axis": [1, 2], "values": [1e-9], "metadata": {}},
        ],
    }
    with pytest.raises(ValueError):
        export_round(tmp_path, frame)
    assert not list(tmp_path.iterdir())
