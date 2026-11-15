"""Describe the measured interval independently of the visible or estimated grid."""

from dataclasses import replace

import numpy as np

from .instruments import DeviceError
from .oscilloscope import ScopeDisplay


def coverage(measurement, expected_span=None):
    x = np.asarray(measurement.axis)
    y = np.asarray(measurement.values)
    if (
        measurement.axis_unit != "s"
        or x.ndim != 1
        or x.shape != y.shape
        or not x.size
        or not np.isfinite(x).all()
    ):
        raise DeviceError("波形缺少有效时间轴", "waveform_unavailable")
    first, last = float(x[0]), float(x[-1])
    steps = np.diff(x)
    dt = (last - first) / len(steps) if steps.size else 0
    if steps.size and (
        dt <= 0 or not np.allclose(steps, dt, rtol=1e-4, atol=8 * np.spacing(max(abs(first), abs(last))))
    ):
        raise DeviceError("波形采样间隔不一致", "waveform_unavailable")
    meta = measurement.metadata
    tolerance = 1.51 * dt
    partial = not steps.size or bool(meta.get("growing_prefix") or meta.get("sweep_rollover"))
    # A stopped RTM record is clipped to its transfer grid on each side.
    # Keep the header and payload extent checks independent of screen rounding.
    screen_tolerance = tolerance
    if "scope_display" in meta:
        screen = ScopeDisplay.parse(meta["scope_display"])
        expected_span = screen.seconds_per_div * screen.horizontal_divisions
        headers = meta.get("headers_observed", ())
        if len(headers) == 2 and headers[0] == headers[1]:
            screen_tolerance = max(tolerance, min(5 * dt, expected_span * 0.0001))
        partial |= first > screen.time_center - expected_span / 2 + screen_tolerance
        partial |= last < screen.time_center + expected_span / 2 - screen_tolerance
    if expected_span is not None:
        partial |= last - first + 2 * screen_tolerance < expected_span
    if "preamble" in meta and isinstance(meta["preamble"], (list, tuple)):
        partial |= len(x) != int(meta["preamble"][2])
    return dict(first_s=first, last_s=last, interval_s=dt, points=len(x), partial=bool(partial))


def mark_coverage(batch, expected_span=None, *, require_full=False):
    measurements = []
    for m in batch.measurements:
        info = coverage(m, expected_span)
        if require_full and info["partial"]:
            raise DeviceError(
                f"CH{m.channel} 仅收到部分波形（{info['first_s']:.9g} 至 {info['last_s']:.9g} s）",
                "incomplete_waveform",
            )
        measurements.append(
            replace(
                m,
                metadata={
                    **m.metadata,
                    "coverage": info,
                    "waveform_status": "部分波形" if info["partial"] else "波形",
                },
            )
        )
    return replace(batch, measurements=tuple(measurements))
