"""DH-8318 single-channel load control, manual V1.1 pp. 7 and 22–26."""

import re

from lab_control.domain.instruments import DeviceError, Parameter
from lab_control.instruments.base import command, enum, number, parameter

NUMERIC = re.compile(rb"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[Ee][+-]?[0-9]+)?")

MODES = {"CC": "CURR", "CV": "VOLT", "CP": "POW", "CR": "RES"}
MODE_REPLIES = {
    "CURR": "CC",
    "CURRENT": "CC",
    "VOLT": "CV",
    "VOLTAGE": "CV",
    "POW": "CP",
    "POWER": "CP",
    "RES": "CR",
    "RESISTANCE": "CR",
    "DYN": "DYN",
    "DYNAMIC": "DYN",
    "LED": "LED",
    "OCP": "OCP",
}
SETPOINTS = {
    "current": ("CC", "CURR", parameter("A", 0, 30)),
    "voltage": ("CV", "VOLT", parameter("V", 0, 150)),
    "load_power": ("CP", "POW", parameter("W", 0, 150)),
    "resistance": ("CR", "RES", parameter("Ω", 0.05, 50000)),
}


def control_commands():
    return {
        "load_input": command("load_input", "INP", Parameter("boolean"), confirm=True),
        "load_mode": command("load_mode", "MODE", enum(*MODES), confirm=True),
        **{name: command(name, path, param, confirm=True) for name, (_, path, param) in SETPOINTS.items()},
    }


def decode_setting(operation, raw):
    text = raw.decode("ascii").upper()
    if operation == "load_input" and text in ("0", "1"):
        return text == "1"
    if operation == "load_mode" and (text in MODES or text in MODE_REPLIES):
        return MODE_REPLIES.get(text, text)
    if operation in SETPOINTS and NUMERIC.fullmatch(raw):
        return number(raw)
    raise DeviceError(f"DH-8318 设置读回无效：{text!r}", "parse_error")
