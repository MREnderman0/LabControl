"""Decode a complete N9030A trace-1 FETCh response in the current format."""

import hashlib
from typing import Any

import numpy as np

from lab_control.domain.instruments import DeviceError, Measurement
from lab_control.instruments.base import number
from lab_control.transport.base import numeric_values

UNITS = {
    "DBM": "dBm",
    "DBMV": "dBmV",
    "DBUV": "dBµV",
    "DBUA": "dBµA",
    "DBMA": "dBmA",
    "DBPW": "dBpW",
    "V": "V",
    "W": "W",
    "A": "A",
    "VOLT": "V",
    "WATT": "W",
    "AMP": "A",
}


def token(raw: bytes) -> str:
    try:
        value = raw.decode("ascii").strip().strip('"').upper()
    except UnicodeError as exc:
        raise DeviceError("Invalid N9030A text response", "parse_error") from exc
    if not value:
        raise DeviceError("Empty N9030A response", "parse_error")
    return value


def scalar(raw: bytes) -> float:
    value = number(raw)
    if abs(value) >= 9e37:
        raise DeviceError("N9030A returned an unavailable value", "parse_error")
    return value


def point_count(raw: bytes) -> int:
    value = scalar(raw)
    if not value.is_integer() or not 2 <= value <= 4_000_000:
        raise DeviceError("Invalid N9030A sweep point count", "parse_error")
    return int(value)


def trace_data(raw: bytes, config: dict[str, Any]) -> np.ndarray:
    form = config["transfer_format"].replace(" ", "")
    if form in {"ASC", "ASCII", "ASC,0", "ASCII,0", "ASC,8", "ASCII,8"}:
        if raw.startswith(b"#"):
            raise DeviceError("N9030A binary reply in ASCII mode", "parse_error")
        values = numeric_values(raw)
    else:
        order = {"NORM": ">", "NORMAL": ">", "SWAP": "<", "SWAPPED": "<"}.get(config["byte_order"])
        dtype = {"REAL,32": "f4", "REAL,64": "f8"}.get(form)
        if not dtype or not order or not raw.startswith(b"#"):
            raise DeviceError("Invalid N9030A trace format or binary framing", "parse_error")
        values = numeric_values(raw, dtype=order + dtype)
    if values.size < 4 or values.size % 2 or np.any(np.abs(values) >= 9e37):
        raise DeviceError("Invalid or unavailable N9030A XY pairs", "parse_error")
    return values.reshape(-1, 2)


def spectrum(raw: bytes, pairs: np.ndarray, config: dict[str, Any]) -> Measurement:
    x, y = pairs[:, 0], pairs[:, 1]
    if len(x) != config["sweep_points"] or np.any(x[1:] <= x[:-1]):
        raise DeviceError("N9030A trace has wrong point count or non-increasing axis", "parse_error")
    unit = UNITS.get(config["amplitude_unit"])
    if not unit:
        raise DeviceError(f"Unsupported N9030A Y unit: {config['amplitude_unit']}", "unsupported")
    if config["span"] < 0:
        raise DeviceError("Invalid N9030A span", "parse_error")
    return Measurement(
        "spectrum",
        unit,
        y,
        axis=x,
        axis_unit="s" if config["span"] == 0 else "Hz",
        metadata={"trace": 1, "points": len(x), "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()},
    )
