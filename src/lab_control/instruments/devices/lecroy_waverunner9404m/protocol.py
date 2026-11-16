"""WaveRunner 9404M trigger and grouped scalar replies."""

from __future__ import annotations

from lab_control.domain.instruments import DeviceError
from lab_control.instruments.common.lecroy_waveform import (
    choice,
    numeric,
    response,
    transfer_setup,
    trigger_setup,
    trigger_source,
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


def grouped_scalars(raw: bytes, queries: tuple[str, ...]) -> tuple[bytes, ...]:
    """Qualified SHORT headers make field order explicit, even for equal values."""
    fields = response(raw).split(";")
    if len(fields) != len(queries):
        raise DeviceError("Grouped reply field count differs", "parse_error")
    for field, query in zip(fields, queries, strict=True):
        if not field.startswith(query.removesuffix("?") + " "):
            raise DeviceError("Grouped reply header/order differs", "parse_error")
    return tuple(field.encode("ascii") for field in fields)
