"""Evaluate numeric watch values with the same metric functions used for MCP recordings."""

import time
from dataclasses import dataclass, field
from math import isfinite

import numpy as np

from lab_control.application.monitoring_state import MonitorFrame
from lab_control.application.power_window import PowerWindow
from lab_control.application.temperature_window import TemperatureWindow
from lab_control.domain.frames import validate_displayed_batch
from lab_control.domain.instruments import Quality, ValidationError
from lab_control.domain.metrics import MetricInput
from lab_control.domain.waveform_coverage import coverage


@dataclass(frozen=True)
class Reading:
    value: float
    unit: str
    quantity: str
    timestamp: str
    received: float
    simulated: bool = False


def select_sample(batch, channel, source):
    samples = [
        m
        for m in batch.measurements
        if m.channel == channel
        and (
            not (source.measurement or source.quantity)
            or m.quantity == (source.measurement or source.quantity)
        )
    ]
    if len(samples) != 1:
        return None
    return samples[0]


def scalar(frame, channel, source):
    m = select_sample(frame.batch, channel, source)
    if m is None or m.axis is not None or m.quality not in (Quality.GOOD, Quality.SIMULATED):
        return None
    values = np.asarray(m.values)
    if values.size != 1 or values.dtype.kind not in "fiu" or not isfinite(float(values.item())):
        return None
    return Reading(
        float(values.item()),
        m.unit,
        m.quantity,
        m.metadata.get("received_at") or frame.batch.timestamp,
        frame.received_at,
        m.quality == Quality.SIMULATED,
    )


def compute_metric(spec, frame, channel, parameters, window):
    profile = spec.profile
    batch = frame.batch
    validate_displayed_batch(batch)
    samples = [
        m
        for m in batch.measurements
        if m.channel == channel
        and (spec.source.kind not in ("trace", "timed_phase") or m.quantity == "phase_noise")
    ]
    if len(samples) != 1:
        raise ValidationError("等待兼容数据")
    m = samples[0]
    if spec.source.kind == "waveform" and (
        m.metadata.get("coverage", {}).get("partial") or coverage(m)["partial"]
    ):
        raise ValidationError("等待完整波形")
    if spec.source.kind == "timed_phase" and not (
        batch.configuration.get("final") and batch.configuration.get("complete")
    ):
        raise ValidationError("等待完整相噪数据")
    if batch.configuration.get("complete") is False:
        raise ValidationError("等待完整数据")
    if window is not None:
        values, unit = window

        def frames():
            for v in values:
                yield {
                    "measurements": [
                        {
                            "channel": channel,
                            "quantity": m.quantity,
                            "unit": unit,
                            "axis": None,
                            "values": np.asarray([v]),
                        }
                    ]
                }
    else:

        def frames():
            yield {
                "configuration": batch.configuration,
                "measurements": [
                    {
                        "channel": channel,
                        "quantity": m.quantity,
                        "unit": m.unit,
                        "axis": m.axis,
                        "axis_unit": m.axis_unit,
                        "values": np.asarray(m.values),
                    }
                ],
            }

    inputs = MetricInput("", {"kind": "single"}, True, frames, lambda: None, lambda *_: None)
    result = profile.process(inputs, profile.parameters.model_validate(parameters))
    readings = {}
    for metric in profile.metrics:
        key = f"ch{channel}.{metric.name}" if profile.per_channel_metrics else metric.name
        v = result[key]
        if v.quality != "unavailable" and v.value is not None and isfinite(v.value):
            readings[metric.name] = Reading(
                v.value,
                metric.unit or m.unit,
                profile.id,
                m.metadata.get("received_at") or batch.timestamp,
                frame.received_at,
                m.quality == Quality.SIMULATED,
            )
    return readings


@dataclass
class RecordedWindows:
    """Collect every direct recording sample, including samples between UI repaints."""

    session: str
    version: int
    sequence: int = 0
    power: PowerWindow = field(default_factory=PowerWindow)
    temperature: TemperatureWindow = field(default_factory=TemperatureWindow)

    def add(self, event):
        self.sequence += 1
        frame = MonitorFrame(
            self.sequence, self.version, event["request"], event["batch"], event["received_at"], self.session
        )
        self.power.add(frame)
        self.temperature.add(frame)

    def snapshot(self, optical):
        now = time.perf_counter()
        return self.power.snapshot(self.session, now) if optical else self.temperature.snapshot(now)
