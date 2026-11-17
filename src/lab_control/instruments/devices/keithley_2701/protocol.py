"""Channel lists and channel-tagged ASCII readings from the 2701."""

import math
import re

from lab_control.domain.instruments import DeviceError, Measurement, utc_now

# Unit tags are part of the reading itself (2701 User's Manual, Fig. 14-1).
UNITS = {
    "VDC": ("voltage", "V", "VOLT:DC"),
    "VAC": ("voltage", "V", "VOLT:AC"),
    "OHM": ("resistance", "ohm", "RES"),
    "OHM4W": ("resistance", "ohm", "FRES"),
    "C": ("temperature", "°C", "TEMP"),
    "F": ("temperature", "°F", "TEMP"),
    "K": ("temperature", "K", "TEMP"),
    "HZ": ("frequency", "Hz", "FREQ"),
    "SECS": ("period", "s", "PER"),
}


def channel_list(text: str) -> tuple[int, ...]:
    match = re.fullmatch(r"\(@([0-9:, ]*)\)", text.strip())
    if not match:
        raise DeviceError(f"Invalid 2701 channel list: {text!r}", "parse_error")
    channels: list[int] = []
    if not match[1].strip():
        return ()
    for item in match[1].strip().split(","):
        limits = item.strip().split(":")
        if len(limits) > 2 or not all(value.isdecimal() for value in limits):
            raise DeviceError(f"Invalid channel range: {item!r}", "parse_error")
        first, last = int(limits[0]), int(limits[-1])
        if not 100 <= first <= last < 300 or first // 100 != last // 100:
            raise DeviceError(f"Invalid slot or channel range: {item!r}", "parse_error")
        channels.extend(range(first, last + 1))
    if len(set(channels)) != len(channels):
        raise DeviceError("Duplicate channels in 2701 scan list", "parse_error")
    return tuple(channels)


def tagged_reading(raw: str, channel: int, function: str) -> Measurement:
    match = re.fullmatch(
        r"([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:E[+-]?\d+)?)[ \t]*([A-Z][A-Z0-9]*)?,\s*\+?(\d{3})(?:INTCHAN|CHAN)?",
        raw.strip().upper(),
    )
    if not match or int(match[3]) != channel:
        raise DeviceError(f"Invalid or misrouted reading for {channel}: {raw!r}", "readback_mismatch")
    value = float(match[1])
    if not math.isfinite(value) or abs(value) >= 9e36:
        raise DeviceError(f"Channel {channel} overload", "overload")
    spec = UNITS.get(match[2])
    if not spec or function != spec[2]:
        raise DeviceError(f"Channel {channel}: {match[2]} does not match {function}", "readback_mismatch")
    return Measurement(
        spec[0],
        spec[1],
        (value,),
        channel,
        metadata={"function": function, "instrument_unit": match[2], "received_at": utc_now()},
    )
