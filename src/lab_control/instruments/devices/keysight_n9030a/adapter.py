"""Swept SA parameter control and passive PXA trace acquisition."""

import math
from dataclasses import replace
from typing import Any

from lab_control.domain.instruments import (
    AcquisitionRequest,
    DeviceCommand,
    DeviceError,
    MeasurementBatch,
    ParameterReading,
    Unsupported,
    ValidationError,
    utc_now,
)
from lab_control.domain.readback import boolean_value
from lab_control.domain.spectrum import FREQUENCY
from lab_control.domain.spectrum_display import panel_scale
from lab_control.instruments.spectrum_panel import SpectrumPanelAdapter
from lab_control.transport.diagnostics import parsing

from .profile import TRACE_QUERY
from .protocol import spectrum, trace_data

FRAME_SETTINGS = (
    "start_frequency",
    "stop_frequency",
    "center_frequency",
    "span",
    "sweep_time",
    "sweep_points",
    "amplitude_unit",
    "transfer_format",
    "byte_order",
)


class N9030aAdapter(SpectrumPanelAdapter):
    optional_firmware_evidence = "exact"
    acquisition_revision = "n9030a-swept-sa-xy-v1"

    def frequency_bounds(self):
        # The analyzer reports limits for the installed option and active input/mixer.
        low = float(self.transport.query("FREQ:STAR? MIN"))
        high = float(self.transport.query("FREQ:STOP? MAX"))
        if not (math.isfinite(low) and math.isfinite(high) and 10 <= high - low < 1e15):
            raise DeviceError("N9030A 返回的频率范围无效", "invalid_parameter")
        return low, high

    def refresh_capabilities(self):
        for name in FREQUENCY:
            self.dynamic_parameters.pop(name, None)
        if super().read("measurement_mode") not in {"SAN", "SANALYZER"}:
            return
        low, high = self.frequency_bounds()
        for name in FREQUENCY:
            bounds = (0, high - low) if name == "span" else (low, high)
            self.dynamic_parameters[name] = replace(
                self.profile.commands[name].capability.parameters["value"],
                minimum=bounds[0],
                maximum=bounds[1],
                range_source="instrument",
            )

    def capabilities(self):
        caps = super().capabilities()
        return replace(
            caps,
            operations=tuple(
                replace(cap, writable=False, reason="等待 Swept SA 输入频率范围")
                if cap.name in FREQUENCY and cap.name not in self.dynamic_parameters
                else cap
                for cap in caps.operations
            ),
        )

    def check_control_context(self, operation: str) -> None:
        if super().read("measurement_mode") not in {"SAN", "SANALYZER"}:
            raise Unsupported("N9030A 频谱参数需要 Swept SA 模式")
        if operation == "reference_level" and super().read("amplitude_unit") != "DBM":
            raise Unsupported("N9030A 参考电平输入需要面板使用 dBm")

    def read(self, operation: str, channel: int = 1) -> Any:
        spec = self.profile.commands.get(operation)
        if spec and spec.capability.writable and operation not in FREQUENCY:
            self.check_control_context(operation)
        return super().read(operation, channel)

    def preflight(self, command: DeviceCommand, parameters: dict[str, Any]) -> None:
        op = command.operation
        self.check_control_context(op)
        if op in FREQUENCY:
            low, high = self.frequency_bounds()
            value = parameters["value"]
            if op in {"start_frequency", "stop_frequency"}:
                start = value if op == "start_frequency" else self.read("start_frequency")
                stop = value if op == "stop_frequency" else self.read("stop_frequency")
                if stop - start < 10:
                    raise ValidationError("普通扫宽至少 10 Hz；零扫宽请设置扫宽为 0")
            else:
                span = value if op == "span" else self.read("span")
                center = value if op == "center_frequency" else self.read("center_frequency")
                if 0 < span < 10:
                    raise ValidationError("扫宽必须为 0 或至少 10 Hz")
                start, stop = center - span / 2, center + span / 2
            if not low <= start <= stop <= high:
                raise ValidationError("目标频段超出当前输入和选件的范围")
        if op in {"rbw", "vbw", "attenuation"} and boolean_value(super().read(op + "_auto")) is not False:
            raise ValidationError("请先关闭对应参数的自动模式")

    def read_panel_scale(self, config, unit):
        mode = self.read_scale_mode("DISP:WIND:TRAC:Y:SPAC?")
        reference = self.scale_value("DISP:WIND:TRAC:Y:RLEV?")
        span = 10 * self.scale_value("DISP:WIND:TRAC:Y:PDIV?") if mode == "log" else None
        return panel_scale(unit, reference, span_db=span)

    def acquire_trace(self, request: AcquisitionRequest) -> MeasurementBatch:
        request.validate()
        if request.kind != "spectrum" or request.initiate:
            raise Unsupported("N9030A supports passive spectrum acquisition")
        if request.channels != (1,):
            raise ValidationError("N9030A acquisition requires trace 1")
        if self.identity is None:
            raise DeviceError("Identify before acquiring an N9030A trace", "identity_required")
        mode = self.read("measurement_mode")
        if mode not in {"SAN", "SANALYZER"}:
            raise DeviceError(
                f"Select Swept SA on the N9030A (current measurement: {mode})", "acquisition_setup_blocked"
            )
        before = {"measurement_mode": mode, **{op: self.read(op) for op in FRAME_SETTINGS}}
        raw = self.transport.query(TRACE_QUERY)
        with parsing():
            pairs = trace_data(raw, before)
        after_mode = self.read("measurement_mode")
        if after_mode != mode or any(self.read(op) != before[op] for op in FRAME_SETTINGS):
            raise DeviceError("N9030A settings changed while reading trace 1", "configuration_changed")
        with parsing():
            measurement = spectrum(raw, pairs, before)
        timestamp = utc_now()
        return MeasurementBatch(
            self.identity,
            (measurement,),
            before,
            validation="full",
            settings_timestamps={op: timestamp for op in before},
        )

    def acquisition_readings(self, batch: MeasurementBatch) -> tuple[ParameterReading, ...]:
        return tuple(
            ParameterReading(op, 1, batch.configuration[op], "instrument", batch.settings_timestamps[op])
            for op in ("start_frequency", "stop_frequency", "center_frequency", "span")
        )
