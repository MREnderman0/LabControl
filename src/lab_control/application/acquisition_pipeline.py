"""Compatibility exports for frame contracts and application cadence policy."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from lab_control.domain.frames import (
    FrozenDict as FrozenDict,
)
from lab_control.domain.frames import (
    batch_bytes as batch_bytes,
)
from lab_control.domain.frames import (
    immutable_batch as immutable_batch,
)
from lab_control.domain.frames import (
    validate_displayed_batch as validate_displayed_batch,
)
from lab_control.domain.frames import (
    validate_recorded_batch as validate_recorded_batch,
)
from lab_control.domain.instruments import MeasurementBatch, ValidationError

SPECTRUM_INTERVAL = "spectrum_interval"


def validate_cadence(
    interval: float, cadence: str | None = None, *, minimum: float = 0.05, spectrum: bool = False
) -> None:
    # Omitted cadence is the legacy API, whose shorter intervals remain supported.
    if spectrum and cadence == SPECTRUM_INTERVAL:
        if type(interval) not in (int, float) or not 0 <= interval <= 60:
            raise ValidationError("频谱采集间隔必须在 0～60 秒内")
        return
    if cadence not in {None, "fastest", "interval"}:
        raise ValidationError("Unknown acquisition cadence")
    lower = minimum if cadence is None else 0.5 if cadence == "fastest" else 0.0
    if isinstance(interval, bool) or not isinstance(interval, (int, float)) or not lower <= interval <= 60:
        raise ValidationError(f"Acquisition interval must be {lower}..60 s")


def stamp_host_read(
    batch: MeasurementBatch,
    started: float,
    started_utc: str,
    completed: float,
    completed_utc: str,
    performance: dict[str, Any] | None = None,
) -> MeasurementBatch:
    """Attach the actual worker read boundaries without changing instrument sample axes."""
    return replace(
        batch,
        timestamp=completed_utc,
        time_basis="host_utc_at_read_completed; waveform axes from instrument",
        timing={
            **batch.timing,
            "version": 1,
            "host_read_started_at": started_utc,
            "host_read_completed_at": completed_utc,
            "read_duration_seconds": completed - started,
            "clock_source": "host_utc; durations=monotonic",
        },
        performance={
            **batch.performance,
            **(performance or {}),
            "acquisition_seconds": completed - started,
            "received_at_monotonic": completed,
        },
    )
