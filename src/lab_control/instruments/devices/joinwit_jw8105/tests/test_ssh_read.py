"""SSH reader contract for fresh, channel-scoped JW8105 readings."""

import asyncio
from types import SimpleNamespace

import pytest

from lab_control.domain.instruments import DeviceError
from lab_control.instruments.devices.joinwit_jw8105.ssh_read import read_power, target


def grant(device="asset", resource="COM10", channels=(1, 9, 16)):
    return {
        "id": "read-scope",
        "devices": {device: {"adapter": "joinwit.jw8105", "model": "JW8105", "resource": resource}},
        "actions": ["view", "connect", "read_parameters"],
        "channels": channels,
    }


class Desktop:
    def __init__(self, *, session=True, source="instrument", simulation=False):
        self.session = session
        self.source = source
        self.simulation = simulation
        self.calls = []

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        if name == "get_server_info":
            data = {"simulation": self.simulation, "grants": [grant()]}
        elif name == "get_device_state":
            data = {"session": {"connectionState": "connected"} if self.session else {}}
        elif name == "connect_device":
            assert args["configure_acquisition"] is False
            data = {"id": "task", "state": "accepted"}
        elif name == "get_task_status":
            data = {"id": "task", "state": "completed"}
        elif name == "read_parameters":
            assert args["operations"] == ["display_power"]
            data = [
                {
                    "operation": "display_power",
                    "channel": args["channel"],
                    "value": -24.125,
                    "source": self.source,
                    "timestamp": "2026-01-01T00:00:00+00:00",
                    "quality": "good",
                    "error": "",
                }
            ]
        else:
            raise AssertionError(name)
        return SimpleNamespace(is_error=False, structured_content={"ok": True, "data": data})


@pytest.mark.parametrize("session", [True, False])
def test_live_read_reuses_or_opens_only_a_passive_session(session):
    desktop = Desktop(session=session)
    result = asyncio.run(read_power(desktop, "com10", 9))
    assert (result["port"], result["channel"], result["power"], result["unit"]) == (
        "COM10",
        9,
        -24.125,
        "dBm",
    )
    assert [n for n, _ in desktop.calls] == [
        "get_server_info",
        "get_device_state",
        *([] if session else ["connect_device", "get_task_status"]),
        "read_parameters",
    ]


@pytest.mark.parametrize("channel", [0, 17, True])
def test_invalid_channel_never_calls_the_agent(channel):
    desktop = Desktop()
    with pytest.raises(DeviceError, match="1 到 16"):
        asyncio.run(read_power(desktop, "COM10", channel))
    assert not desktop.calls


def test_only_the_selected_port_and_granted_channels_can_be_read():
    with pytest.raises(DeviceError) as refused:
        target([grant(resource="COM11"), grant(channels=[1])], "COM10", 16)
    assert refused.value.code == "no_matching_grant"
    with pytest.raises(DeviceError) as ambiguous:
        target([grant(), grant(device="other")], "COM10", 9)
    assert ambiguous.value.code == "identity_conflict"


@pytest.mark.parametrize(
    "kwargs,code", [({"source": "cached"}, "invalid_readback"), ({"simulation": True}, "simulation")]
)
def test_cache_and_simulation_are_not_current_optical_power(kwargs, code):
    with pytest.raises(DeviceError) as rejected:
        asyncio.run(read_power(Desktop(**kwargs), "COM10", 9))
    assert rejected.value.code == code


class MonitoringDesktop(Desktop):
    def __init__(self, include_channel=True):
        super().__init__()
        self.sequences = iter([12, 13])
        self.include_channel = include_channel

    async def call_tool(self, name, args):
        if name == "get_device_state":
            data = {
                "session": {"connectionState": "busy", "sessionId": "session"},
                "monitoring": {"running": True, "sequence": 12},
            }
        elif name == "get_latest_measurement":
            seq = next(self.sequences)
            sample = {
                "channel": args["channel"],
                "quality": "good",
                "points": 1,
                "quantity": "power",
                "unit": "dBm",
                "preview": [-float(seq)],
            }
            data = {
                "available": True,
                "session_id": "session",
                "sequence": seq,
                "timestamp": "2026-01-01T00:00:00+00:00",
                "measurements": [sample] if self.include_channel else [],
            }
        else:
            return await super().call_tool(name, args)
        self.calls.append((name, args))
        return SimpleNamespace(is_error=False, structured_content={"ok": True, "data": data})


def test_running_monitor_waits_for_a_new_frame_and_never_reopens_serial():
    desktop = MonitoringDesktop()
    assert asyncio.run(read_power(desktop, "COM10", 16))["power"] == -13
    assert [name for name, _ in desktop.calls] == [
        "get_server_info",
        "get_device_state",
        "get_latest_measurement",
        "get_latest_measurement",
    ]


def test_unmonitored_channel_is_not_returned_from_another_frame():
    desktop = MonitoringDesktop(include_channel=False)
    with pytest.raises(DeviceError) as exc:
        asyncio.run(read_power(desktop, "COM10", 16))
    assert exc.value.code == "channel_not_monitored"
