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
from lab_control.domain.readback import boolean_value
from lab_control.domain.spectrum_display import panel_scale
from lab_control.instruments.base import Profile, command, number, parameter, read_command
from lab_control.instruments.spectrum_panel import SpectrumPanelAdapter

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
    for key, path, param in (
        ("rbw", ":BAND", parameter("Hz", 1, 20e6)),
        ("vbw", ":BAND:VID", parameter("Hz", 1, 20e6)),
        ("reference_level", ":DISP:WIND:TRAC:Y:RLEV", parameter("dBm", -150, 30)),
        (
            "attenuation",
            ":POW:ATT",
            Parameter("integer", "dB", 0, 30, choices=tuple(str(n) for n in range(0, 31, 2))),
        ),
    ):
        commands[key] = replace(command(key, path, param, scoped=False), decode=number)
        if key != "reference_level":
            name = key + "_auto"
            commands[name] = command(name, path + ":AUTO", Parameter("boolean"), scoped=False)
    for key, path, decode in (
        ("measurement_mode", ":INST?", token),
        ("available_modes", ":INST:CAT?", token),
        ("transfer_format", ":FORM?", token),
        ("sweep_points", ":SWE:POIN?", count),
        ("amplitude_unit", ":AMPL:UNIT?", token),
        ("trace_mode", ":TRAC1:TYPE?", token),
        ("detector", ":DET:FUNC?", token),
        ("sweep_time", ":SWE:TIME?", number),
    ):
        spec = read_command(key, path, scoped=False)
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


class MicrowaveAnalyzer(SpectrumPanelAdapter):
    # A firmware conflict on the optional query remains suppressed for this connection.
    _sweep_query_rejected = False

    def require_spectrum(self) -> None:
        if super().read("measurement_mode") != "SA":
            raise Unsupported("请在 4959E 面板选择频谱分析（SA）模式")

    def read(self, operation: str, channel: int = 1) -> Any:
        self._context(channel, {})
        if operation not in ("measurement_mode", "available_modes"):
            self.require_spectrum()
        if operation == "reference_level" and super().read("amplitude_unit") != "DBM":
            raise Unsupported("4959E 参考电平输入需要面板使用 dBm")
        return super().read(operation, channel)

    def preflight(self, cmd: DeviceCommand, parameters: dict[str, Any]) -> None:
        self.require_spectrum()
        if cmd.operation == "reference_level" and super().read("amplitude_unit") != "DBM":
            raise Unsupported("4959E 参考电平输入需要面板使用 dBm")
        if cmd.operation in {"rbw", "vbw", "attenuation"}:
            if boolean_value(super().read(cmd.operation + "_auto")) is not False:
                raise ValidationError("请先关闭对应参数的自动模式")
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
                "center_frequency",
                "span",
                "transfer_format",
                "start_frequency",
                "stop_frequency",
                "sweep_points",
                "trace_mode",
                "detector",
                "rbw",
            )
        }
        # Only issue the optional list-sweep query for an asset with a
        # confirmed S13 license. It raises a modal error on other 4959Es.
        if (
            self.identity
            and self.identity.serial == self.config.expected_serial
            and "4959-S13" in self.config.options
            and not self._sweep_query_rejected
        ):
            sweep_mode = token(self.transport.query(":SWE:MODE?"))
            if sweep_mode in {"LINE", "0", "LIST", "1"}:
                settings["sweep_mode"] = "LINE" if sweep_mode in {"LINE", "0"} else "LIST"
            else:
                self._sweep_query_rejected = True
        if settings["start_frequency"] == settings["stop_frequency"]:
            settings["sweep_time"] = read("sweep_time") / 1000  # SA reply: ms; measurement axis: s.
        settings["measurement_mode"] = mode
        return settings

    def read_panel_scale(self, config, unit):
        # TRAC1:DATA? always returns dBm. Reference level is in the front-panel
        # amplitude unit; the SA offset query returns dB (not VSA RPOS).
        mode = token(self.transport.query(":AMPL:SCAL?"))
        if mode not in {"0", "LOG", "1", "LINE", "LIN"}:
            raise ValueError("Unknown 4959E scale type")
        units = ("dBm", "dBmV", "dBµV", "V", "W", "A", "dBW", "dBV", "dBA", "dBmA", "dBµA")
        names = ("DBM", "DBMV", "DBUV", "V", "W", "A", "DBW", "DBV", "DBA", "DBMA", "DBUA")
        key = token(self.transport.query(":AMPL:UNIT?"))
        name = key if key in names else next((n for i, n in enumerate(names) if str(i) == key), "")
        if not name:
            raise ValueError("Unknown 4959E amplitude unit")
        panel_unit = units[names.index(name)]
        level = self.scale_value(":DISP:WIND:TRAC:Y:RLEV?")
        offset = self.scale_value(":DISP:WIND:TRAC:Y:OFFS?")
        span = 10 * self.scale_value(":DISP:WIND:TRAC:Y:PDIV?") if mode in {"0", "LOG"} else None
        return panel_scale(panel_unit, level, span_db=span, offset_db=offset, impedance=50)

    def acquire_trace(self, request: AcquisitionRequest) -> MeasurementBatch:
        batch = super().acquire_trace(request)
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
        mode = settings.get("sweep_mode")
        if mode is not None:
            axis_source = "instrument_sweep_mode"
        elif (
            self.identity is not None
            and self.identity.serial == self.config.expected_serial
            and bool(self.config.expected_serial)
            and "no_list_sweep" in self.config.options
            and "4959-S13" not in self.config.options
        ):
            axis_source = "verified_linear_only"
        elif request.panel_linear_sweep:  # Retain provenance on older explicit requests.
            axis_source = "operator_confirmed_linear"
        else:
            axis_source = "linear_default"
        consistent = np.isclose(settings["span"], stop - start, rtol=1e-8, atol=1e-3) and np.isclose(
            settings["center_frequency"], (start + stop) / 2, rtol=1e-9, atol=1e-3
        )
        double = values.size == 2 * points
        layout_verified = not double or (
            self.identity is not None
            and self.identity.firmware == "2.6.14"
            and bool(np.all(values[:points] >= values[points:]))
        )
        if mode == "LIST" or not consistent or not layout_verified:
            axis, unit, axis_kind = np.arange(values.size), "index", "transfer_sample_index"
        else:
            settings["sweep_mode_source"] = axis_source
            axis, unit = (
                (np.linspace(0, settings["sweep_time"], points), "s")
                if start == stop
                else (np.linspace(start, stop, points), "Hz")
            )
            axis_kind = unit
            if double:
                # Firmware 2.6.14 sends the upper and lower envelopes as two
                # contiguous blocks. Interleave on the same bin for plotting.
                values = np.column_stack((values[:points], values[points:])).ravel()
                axis = np.repeat(axis, 2)
                axis_kind = "per_bin_max_min"
                settings["trace_wire_layout"] = "max_block_then_min_block"
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
                    "axis_kind": axis_kind,
                },
            ),
        )
