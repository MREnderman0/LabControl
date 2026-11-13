"""Channel-addressed programming for the native Keysight E36313A persona."""

from dataclasses import replace

from lab_control.domain.instruments import DeviceError, Parameter, Unsupported
from lab_control.instruments.base import Profile, ScpiAdapter, command, number, parameter, read_command
from lab_control.instruments.common.scalar import acquire_power

SOURCE = "https://www.keysight.com/us/en/assets/9018-04577/programming-guides/9018-04577.pdf"


def numeric(raw: bytes) -> float:
    value = number(raw)
    if abs(value) >= 9e37:
        raise DeviceError("电源未返回有效数值", "overload")
    return value


def on_off(raw: bytes) -> bool:
    state = raw.strip().upper()
    if state in (b"1", b"ON"):
        return True
    if state in (b"0", b"OFF"):
        return False
    raise DeviceError("无效的电源开关读回", "parse_error")


def coupled_channels(raw: bytes) -> set[int]:
    names = {"P6V": 1, "P25V": 2, "N25V": 3, "CH1": 1, "CH2": 2, "CH3": 3}
    selection = raw.decode("ascii").strip().upper()
    if selection == "NONE":
        return set()
    if selection == "ALL":
        return {1, 2, 3}
    try:
        return {names[item.strip()] for item in selection.split(",")}
    except KeyError as exc:
        raise DeviceError("无法识别电源同步通道列表", "parse_error") from exc


def profile() -> Profile:
    commands = {}
    for name, path, spec in (
        ("voltage", "VOLT", parameter("V", 0, 25)),
        ("current", "CURR", parameter("A", 0, 10)),
        ("output", "OUTP", Parameter("boolean")),
    ):
        cmd = command(name, path, spec, confirm=name == "output", query=path + "? (@{ch})")
        commands[name] = replace(
            cmd, writes=(path + " {value},(@{ch})",), decode=on_off if name == "output" else numeric
        )
    for name, path in (("measured_voltage", "MEAS:VOLT"), ("measured_current", "MEAS:CURR")):
        commands[name] = replace(read_command(name, path + "? (@{ch})"), decode=numeric)
    return Profile(
        "keysight.e36313a",
        ("Keysight Technologies",),
        ("E36313A",),
        "power_supply",
        (1, 2, 3),
        SOURCE,
        "E36300 Series Programming Guide, CURRent / MEASure / OUTPut / VOLTage",
        commands,
        ("voltage", "current", "output"),
        acquisition="power",
        channel_limits={1: {"voltage": 6}, 2: {"current": 2}, 3: {"current": 2}},
    )


class E36313aAdapter(ScpiAdapter):
    def preflight(self, command, parameters):
        # In these modes an apparently single-channel write can affect another output.
        if command.channel in (2, 3):
            pair = self.transport.query("OUTP:PAIR?").decode("ascii").strip().upper()
            if pair != "OFF" or on_off(self.transport.query("OUTP:TRAC?")):
                raise Unsupported("配对或跟踪模式下未开放 CH2/CH3 独立控制")
        if command.operation == "output":
            coupled = coupled_channels(self.transport.query("OUTP:COUP:CHAN?"))
            if command.channel in coupled and len(coupled) > 1:
                raise Unsupported("该通道与其他输出同步，无法独立切换")

    def acquire_measurements(self, request):
        return acquire_power(self, request)
