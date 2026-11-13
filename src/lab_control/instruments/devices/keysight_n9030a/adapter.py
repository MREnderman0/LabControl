"""Read the PXA's existing trace without reconfiguring or triggering the analyzer."""

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
from lab_control.instruments.base import ScpiAdapter
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


class N9030aAdapter(ScpiAdapter):
    acquisition_revision = "n9030a-swept-sa-xy-v1"

    def check_control_context(self, operation: str) -> None:
        if super().read("measurement_mode") not in {"SAN", "SANALYZER"}:
            raise Unsupported("N9030A 频谱参数需要 Swept SA 模式")
        if operation == "reference_level" and super().read("amplitude_unit") != "DBM":
            raise Unsupported("N9030A 参考电平输入需要面板使用 dBm")

    def read(self, operation: str, channel: int = 1) -> Any:
        spec = self.profile.commands.get(operation)
        if spec and spec.capability.writable:
            self.check_control_context(operation)
        return super().read(operation, channel)

    def preflight(self, command: DeviceCommand, parameters: dict[str, Any]) -> None:
        op = command.operation
        self.check_control_context(op)
        if op in {"rbw", "vbw", "attenuation"} and boolean_value(super().read(op + "_auto")) is not False:
            raise ValidationError("请先关闭对应参数的自动模式")

    def acquire(self, request: AcquisitionRequest) -> MeasurementBatch:
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
