"""WaveRunner 9404M trigger and grouped scalar replies."""

from __future__ import annotations

from lab_control.domain.instruments import DeviceError
from lab_control.instruments.common.lecroy_waveform import (
    choice,
    numeric,
    response,
    transfer_setup,
    value_text,
    waveform,
)

__all__ = [
    "choice",
    "grouped_scalars",
    "numeric",
    "response",
    "transfer_setup",
    "trigger_setup",
    "trigger_source",
    "value_text",
    "waveform",
]


def trigger_setup(raw: bytes) -> dict[str, str]:
    """Keep all TRSE qualifiers; a source edit must not reset holdoff."""
    parts = [v.strip().upper() for v in value_text(raw).split(",")]
    if len(parts) < 3 or len(parts) % 2 != 1 or not parts[0]:
        raise DeviceError("Invalid trigger setup", "parse_error")
    result = {"type": parts[0]}
    for key, value in zip(parts[1::2], parts[2::2], strict=True):
        if not key or not value or key in result:
            raise DeviceError("Invalid or duplicate trigger qualifier", "parse_error")
        result[key] = value
    if "SR" not in result:
        raise DeviceError("Missing trigger source", "parse_error")
    return result


def trigger_source(raw: bytes) -> str:
    return trigger_setup(raw)["SR"]


def grouped_scalars(raw: bytes, queries: tuple[str, ...]) -> tuple[bytes, ...]:
    """Qualified SHORT headers make field order explicit, even for equal values."""
    fields = response(raw).split(";")
    if len(fields) != len(queries):
        raise DeviceError("Grouped reply field count differs", "parse_error")
    for field, query in zip(fields, queries, strict=True):
        if not field.startswith(query.removesuffix("?") + " "):
            raise DeviceError("Grouped reply header/order differs", "parse_error")
    return tuple(field.encode("ascii") for field in fields)
