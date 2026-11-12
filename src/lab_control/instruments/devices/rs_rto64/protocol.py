from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np

from lab_control.domain.instruments import (
    AcquisitionRequest,
    DeviceCommand,
    DeviceError,
    Measurement,
    Unsupported,
)
from lab_control.transport.base import block_payload, numeric_values

if TYPE_CHECKING:
    from lab_control.instruments.base import ScpiAdapter


def flag(raw: bytes) -> bool:
    value = raw.strip().upper()
    if value in {b"1", b"ON"}:
        return True
    if value in {b"0", b"OFF"}:
        return False
    raise DeviceError(f"Invalid on/off response: {value!r}", "parse_error")


def export_settings(adapter: ScpiAdapter) -> tuple[str, bool]:
    query = adapter.transport.query
    if flag(query("EXP:WAV:MULT?")):
        raise Unsupported("Multichannel export is active; channel selection cannot be inferred from DATA?")
    fmt = query("FORM?").decode("ascii").strip().upper().replace(" ", "")
    if fmt not in {"ASC", "ASC,0", "ASCII", "ASCII,0", "REAL,32"}:
        raise Unsupported(f"Unqualified RTO waveform transfer format: {fmt}")
    return fmt, flag(query("EXP:WAV:INCX?"))


def acquire(adapter: ScpiAdapter, request: AcquisitionRequest) -> tuple[Measurement, ...]:
    if request.kind != "waveform":
        raise Unsupported("Use acquisition kind=waveform for this model")
    transport = adapter.transport
    if request.initiate:
        applied = adapter.apply(DeviceCommand("single", confirmed=request.confirmed))
        if applied.outcome != "success":
            raise DeviceError(applied.error, "configuration_uncertain")
        transport.query("*OPC?")
    settings = export_settings(adapter)
    fmt, has_x = settings
    result = []
    for channel in request.channels:
        if not flag(transport.query(f"CHAN{channel}:STAT?")):
            raise DeviceError(f"CH{channel} is off", "waveform_unavailable")
        unit = transport.query(f"PROB{channel}:SET:ATT:UNIT?").decode("ascii").strip().upper()
        if unit != "V":
            raise Unsupported(f"CH{channel} probe unit {unit!r} is outside the voltage waveform profile")
        head_query = f"CHAN{channel}:DATA:HEAD?"
        header = numeric_values(transport.query(head_query))
        if header.size != 4 or header[3] != 1 or not 1 <= header[2] <= 4_000_000 or header[2] % 1:
            raise DeviceError("Unsupported record length or envelope waveform", "parse_error")
        start, stop = header[:2]
        count = int(header[2])
        if count > 1 and stop <= start:
            raise DeviceError("Invalid waveform time interval", "parse_error")
        raw = transport.query(f"CHAN{channel}:DATA?")
        if fmt == "REAL,32":
            # RTO floating point transfers use native little-endian IEEE values.
            # With X enabled the time is float64 and the voltage is float32.
            stride = 12 if has_x else 4
            payload = block_payload(raw)
            if len(payload) != count * stride:
                raise DeviceError("Waveform block length disagrees with header", "parse_error")
            if has_x:
                axis = np.ndarray((count,), dtype="<f8", buffer=payload, strides=(stride,))
                values = np.ndarray((count,), dtype="<f4", buffer=payload, offset=8, strides=(stride,))
            else:
                values = np.frombuffer(payload, dtype="<f4")
                axis = np.linspace(start, stop, count)
        else:
            if raw.startswith(b"#"):
                raise DeviceError("Binary waveform with ASCII transfer selected", "parse_error")
            data = numeric_values(raw)
            if data.size != count * (2 if has_x else 1):
                raise DeviceError("Waveform point count disagrees with header", "parse_error")
            values = data[1::2] if has_x else data
            axis = data[::2] if has_x else np.linspace(start, stop, count)
        if not (np.isfinite(values).all() and np.isfinite(axis).all() and np.all(np.diff(axis) > 0)):
            raise DeviceError("Invalid voltage or time values", "parse_error")
        if not np.array_equal(header, numeric_values(transport.query(head_query))):
            raise DeviceError("Waveform header changed during transfer", "configuration_changed")
        result.append(
            Measurement("voltage", "V", values, channel, axis, "s", metadata={"header": header.tolist()})
        )
    if settings != export_settings(adapter):
        raise DeviceError("Waveform export format changed during transfer", "configuration_changed")
    adapter.configuration["transfer"] = fmt
    return tuple(result)
