"""4959E spectrum mode. Connection and measurement reads leave the front panel mode intact."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import numpy as np

from lab_control.domain.instruments import (
    AcquisitionRequest,
    DeviceCommand,
    DeviceError,
    Measurement,
    MeasurementBatch,
    Parameter,
    Unsupported,
    ValidationError,
    utc_now,
)
from lab_control.instruments.base import Profile, ScpiAdapter, command, number, parameter, read_command

SOURCE = "https://www.ceyear.com/Public/Uploads/uploadfile/files/20260709/4959chengkongshouce.pdf"
MAX_HZ = 26.5e9
FREQUENCY = {"start_frequency": "STAR", "stop_frequency": "STOP", "center_frequency": "CENT", "span": "SPAN"}
TRACE_QUERY = ":TRAC1:DATA?"
VISIBLE_TRACES = {"CLRW", "MAXH", "MINH", "VIEW"}


def token(raw: bytes) -> str:
    try:
        value = raw.decode("ascii").strip().strip('"').upper()
    except UnicodeDecodeError as exc:
        raise DeviceError("Invalid 4959E response encoding", "parse_error") from exc
    if not value:
        raise DeviceError("Empty 4959E reply", "parse_error")
    return value


def count(raw: bytes) -> int:
    value = number(raw)
    if not value.is_integer() or not 101 <= value <= 4001:
        raise DeviceError("Invalid 4959E sweep point count", "parse_error")
    return int(value)


def profile() -> Profile:
    commands = {
        key: replace(command(key, ":FREQ:" + path, parameter("Hz", 0, MAX_HZ), scoped=False), decode=number)
        for key, path in FREQUENCY.items()
    }
    scale = command("display_scale", ":DISP:WIND:TRAC:Y:PDIV", parameter("dB/div", 0.2, 40), scoped=False)
    commands["display_scale"] = replace(scale, decode=number)
    for key, path, decode in (
        ("measurement_mode", ":INST?", token),
        ("available_modes", ":INST:CAT?", token),
        ("transfer_format", ":FORM?", token),
        ("sweep_points", ":SWE:POIN?", count),
        ("sweep_mode", ":SWE:MODE?", token),
        ("trace_mode", ":TRAC1:TYPE?", token),
        ("detector", ":DET:FUNC?", token),
        ("sweep_time", ":SWE:TIME?", number),
        ("rbw", ":BAND?", number),
    ):
        spec = read_command(key, path, scoped=False)
        if key == "rbw":
            spec = replace(
                spec, capability=replace(spec.capability, parameters={"value": Parameter(unit="Hz")})
            )
        commands[key] = replace(spec, decode=decode)
    return Profile(
        "ceyear.4959e",
        ("Ceyear Technologies",),
        ("4959E",),
        "spectrum_analyzer",
        (1,),
        SOURCE,
        "4959 系列程控手册 C，第 3.3 节",
        commands,
        ("measurement_mode",),
        acquisition="spectrum",
    )


PROFILE = profile()


def ascii_trace(raw: bytes, points: int, detector: str) -> np.ndarray:
    payload = raw.strip()
    if b"\r" in payload or b"\n" in payload:
        raise DeviceError("4959E returned multiple records for Trace 1", "parse_error")
    parts = payload.split(b",")
    if len(parts) != points and not (detector == "NORM" and len(parts) == points * 2):
        raise DeviceError(f"4959E returned {len(parts)} trace points, expected {points}", "parse_error")
    try:
        values = np.array([float(p) for p in parts], dtype=np.float64)
    except ValueError as exc:
        raise DeviceError("Invalid 4959E ASCII trace", "parse_error") from exc
    if not np.isfinite(values).all() or (np.abs(values) >= 1e30).any():
        raise DeviceError("Invalid or overloaded 4959E trace", "parse_error")
    return values


class MicrowaveAnalyzer(ScpiAdapter):
    def require_spectrum(self) -> None:
        if super().read("measurement_mode") != "SA":
            raise Unsupported("请在 4959E 面板选择频谱分析（SA）模式")

    def read(self, operation: str, channel: int = 1) -> Any:
        self._context(channel, {})
        if operation not in ("measurement_mode", "available_modes"):
            self.require_spectrum()
        return super().read(operation, channel)

    def preflight(self, cmd: DeviceCommand, parameters: dict[str, Any]) -> None:
        self.require_spectrum()
        value = parameters["value"]
        if cmd.operation in {"center_frequency", "span"}:
            center = value if cmd.operation == "center_frequency" else super().read("center_frequency")
            span = value if cmd.operation == "span" else super().read("span")
            if center - span / 2 < 0 or center + span / 2 > MAX_HZ:
                raise ValidationError("4959E center/span exceeds the spectrum frequency range")
        elif cmd.operation in {"start_frequency", "stop_frequency"}:
            opposite = "stop_frequency" if cmd.operation == "start_frequency" else "start_frequency"
            other = super().read(opposite)
            width = other - value if cmd.operation == "start_frequency" else value - other
            if width < 0:
                raise ValidationError("4959E requires ordered sweep endpoints")

    def frame_settings(self) -> dict[str, Any]:
        read = super().read
        mode = read("measurement_mode")
        if mode != "SA":
            return {"measurement_mode": mode}
        settings = {
            key: read(key)
            for key in (
                "sweep_mode",
                "transfer_format",
                "start_frequency",
                "stop_frequency",
                "sweep_points",
                "trace_mode",
                "detector",
                "rbw",
            )
        }
        if settings["start_frequency"] == settings["stop_frequency"]:
            settings["sweep_time"] = read("sweep_time")
        settings["measurement_mode"] = mode
        return settings

    def acquire(self, request: AcquisitionRequest) -> MeasurementBatch:
        batch = super().acquire(request)
        settings = batch.measurements[0].metadata["settings"]
        return replace(
            batch,
            configuration={**batch.configuration, **settings},
            validation="full",
            settings_timestamps=dict.fromkeys(settings, utc_now()),
        )

    def acquire_measurements(self, request: AcquisitionRequest) -> tuple[Measurement, ...]:
        if request.kind != "spectrum" or request.channels != (1,) or request.initiate:
            raise Unsupported("4959E supports passive spectrum trace 1")
        settings = self.frame_settings()
        if settings["measurement_mode"] != "SA":
            raise Unsupported("请在 4959E 面板选择频谱分析（SA）模式")
        index_only = (
            self.identity is not None
            and self.identity.firmware == "2.6.14"
            and settings["sweep_mode"] == "ERROR!"
        )
        if settings["sweep_mode"] != "LINE" and not index_only:
            raise Unsupported("4959E trace acquisition requires a linear sweep")
        if settings["transfer_format"] != "ASC":
            raise Unsupported("4959E trace read requires the qualified ASC transfer format")
        if settings["trace_mode"] not in VISIBLE_TRACES:
            raise Unsupported("4959E trace 1 must be visible with a supported trace type")
        points = settings["sweep_points"]
        start, stop = settings["start_frequency"], settings["stop_frequency"]
        if not 0 <= start <= stop or settings.get("sweep_time", 1) <= 0 or settings["rbw"] <= 0:
            raise DeviceError("Invalid 4959E axis", "parse_error")
        values = ascii_trace(self.transport.query(TRACE_QUERY), points, settings["detector"])
        if settings != self.frame_settings():
            raise DeviceError("4959E configuration changed during trace read", "configuration_changed")
        raw_order = values.size == 2 * points
        if raw_order:
            # The manufacturer specifies both extrema per sweep point, but does not
            # document their order on the wire. Preserve all returned amplitudes.
            axis, unit = np.arange(values.size), "index"
        elif index_only:
            if settings["detector"] not in {"POS", "NEG", "SAMP", "AVER", "RMS"}:
                raise Unsupported("4959E 未确认当前检波模式的单值轨迹")
            axis, unit = np.arange(points), "index"
        elif stop > MAX_HZ:
            raise DeviceError("4959E physical sweep axis exceeds the verified frequency range", "parse_error")
        elif start == stop:
            axis, unit = np.linspace(0, settings["sweep_time"], points), "s"
        else:
            axis, unit = np.linspace(start, stop, points), "Hz"
        return (
            Measurement(
                "spectrum",
                "dBm",
                values,
                1,
                axis,
                unit,
                metadata={
                    "points": int(values.size),
                    "trace": 1,
                    "settings": settings,
                    "axis_kind": "transfer_sample_index" if raw_order else unit,
                },
            ),
        )
