"""Four-wire acquisition must verify both relays and restore global meter settings."""

from dataclasses import replace

import pytest

from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.connections import ConnectionConfig
from lab_control.domain.instruments import DeviceError
from lab_control.instruments.registry import build_scpi_adapter

from ..protocol import channel_list, tagged_reading
from .test_acquisition import REQUEST, Meter


class Thermometer(Meter):
    def __init__(self):
        super().__init__()
        self.settings = {"FUNC": '"VOLT:DC"', "TEMP:TRAN": "TC", "TEMP:FRTD:TYPE": "PT385", "UNIT:TEMP": "F"}
        self.wrong_pair = False

    def write(self, cmd):
        key, _, arg = cmd.partition(" ")
        if key in self.settings:
            self.writes.append(cmd)
            self.settings[key] = arg
            return
        super().write(cmd)
        if key == "ROUT:CLOS" and not self.wrong_pair:
            channel = int(arg[2:-1])
            self.state["closed_relays"] = f"(@{channel},{channel + 20},{channel // 100 * 100 + 41})"

    def query(self, cmd):
        if cmd.endswith("?") and cmd[:-1] in self.settings:
            return self.settings[cmd[:-1]].encode()
        if cmd == "DATA:FRESH?":
            ch = channel_list(self.state["system_channel"])[0]
            if self.reply and ch == 201:
                return self.reply
            return f"+6.18626328E+01 C,{ch}INTCHAN".encode()
        return super().query(cmd)


@pytest.fixture
def bound():
    meter = Thermometer()
    adapter = build_scpi_adapter(ConnectionConfig(meter.resource, "keithley.2701", "mock"), meter)
    adapter.identify()
    settings = {
        "pt100": {"serial": "TEST", "channels": [201, 101], "cards": {"1": meter.serial, "2": meter.serial}}
    }
    adapter.bind_control_state(lambda: settings, None)
    adapter.refresh_capabilities()
    assert not meter.writes
    return adapter, meter


def test_four_wire_celsius_preserves_input_setup_and_scan_list(bound):
    adapter, meter = bound
    before = dict(meter.state), dict(meter.settings)
    assert adapter.capabilities().channels == (201, 101)
    batch = adapter.acquire(REQUEST)
    assert [(m.channel, m.unit, m.values[0], m.metadata["sense_channel"]) for m in batch.measurements] == [
        (101, "°C", 61.8626328, 121),
        (201, "°C", 61.8626328, 221),
    ]
    assert (meter.state, meter.settings) == before


@pytest.mark.parametrize("failure", ["pair", "overload", "fahrenheit", "misroute"])
def test_invalid_temperature_frame_restores_all_settings(bound, failure):
    adapter, meter = bound
    before = dict(meter.state), dict(meter.settings)
    meter.wrong_pair = failure == "pair"
    meter.reply = {
        "overload": b"+9.9E37,201INTCHAN",
        "fahrenheit": b"70 F,201INTCHAN",
        "misroute": b"30 C,101INTCHAN",
    }.get(failure)
    with pytest.raises(DeviceError) as exc:
        adapter.acquire(REQUEST)
    assert exc.value.code == ("overload" if failure == "overload" else "readback_mismatch")
    assert (meter.state, meter.settings) == before


def test_changed_card_and_unbound_channels_cannot_route(bound):
    adapter, meter = bound
    with pytest.raises(DeviceError):
        adapter.acquire(replace(REQUEST, channels=(121,)))
    meter.serial = "9876"
    with pytest.raises(DeviceError):
        adapter.acquire(REQUEST)
    assert meter.writes == []


def test_occupied_relays_pause_monitoring_and_retry_uses_same_session(tmp_path, qtbot):
    meter = Thermometer()
    meter.close = lambda: None
    config = ConnectionConfig("mock://pt100-relays", "keithley.2701", "mock", expected_serial="TEST")
    with ApplicationRuntime(root=tmp_path, hardware=False) as app:
        app.sessions.factory = lambda cfg: build_scpi_adapter(cfg, meter)
        device = app.add(config)
        app.repository.save_workspace(
            f"device-control-state/{device}",
            {
                "pt100": {
                    "serial": "TEST",
                    "cards": {"1": meter.serial, "2": meter.serial},
                    "channels": [101, 201],
                }
            },
        )
        app.connections.request(device, configure=False).result(5)
        session = app.sessions.sessions[device]
        meter.state.update(system_channel="(@201,221)", closed_relays="(@201,221,241)")
        app.monitoring.start(device, (REQUEST,), once=True)
        qtbot.waitUntil(lambda: "线路仍闭合" in app.monitoring.snapshots()[device]["notice"])
        assert session.state == "connected" and not meter.writes
        assert not app.monitoring.latest(device, "scalar")
        meter.state.update(system_channel="(@)", closed_relays="(@)")
        app.monitoring.start(device, (REQUEST,), once=True)
        qtbot.waitUntil(lambda: app.monitoring.latest(device, "scalar") is not None)
        assert app.sessions.sessions[device] is session and session.state == "connected"
        assert all(m.unit == "°C" for m in app.monitoring.latest(device, "scalar").batch.measurements)


def test_valid_channel_with_no_unit_is_never_accepted():
    with pytest.raises(DeviceError):
        tagged_reading("+1.23E01,101INTCHAN", 101, "TEMP")
