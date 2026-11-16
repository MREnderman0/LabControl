"""WaveRunner 9404M cache dependencies and shared MAUI limits."""

from lab_control.instruments.common.lecroy_ranges import (
    EDGE_POINT_RESERVE,
    HORIZONTAL_DIVISIONS,
    MAX_FRAME_BYTES,
    MAX_WAVEFORM_BYTES,
    MEMORY_QUERIES,
    PATHS,
    POINTS_MAX,
    POINTS_MIN,
    cvar_query,
    fixed_rate_timebase,
    plausible_adaptation,
    read_range,
)

__all__ = [
    "DEPENDENCIES",
    "EDGE_POINT_RESERVE",
    "HORIZONTAL_DIVISIONS",
    "MAX_FRAME_BYTES",
    "MAX_WAVEFORM_BYTES",
    "MEMORY_QUERIES",
    "PATHS",
    "POINTS_MAX",
    "POINTS_MIN",
    "cvar_query",
    "fixed_rate_timebase",
    "plausible_adaptation",
    "read_range",
]

DEPENDENCIES = {
    "timebase",
    "time_position",
    "record_length",
    "scale",
    "offset",
    "coupling",
    "probe_attenuation",
    "channel_enabled",
}
