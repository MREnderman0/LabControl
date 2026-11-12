"""Read-only Pangu KT616R adapter for the shared scan monitoring workspace."""

import math
import struct

from lab_control.domain.connections import ConnectionConfig
from lab_control.domain.instruments import (
    AcquisitionRequest,
    ApplyResult,
    Capability,
    CapabilitySet,
    DeviceCommand,
    DeviceError,
    DeviceIdentity,
    DeviceStatus,
    Measurement,
    MeasurementBatch,
    ParameterReading,
    Unsupported,
    ValidationError,
)

from .protocol import CHANNELS, ORDERS, SOURCE, RecorderTransport, Settings

ADAPTER_ID = "pangu.kt616r"
# Observed with all inputs disconnected. Suppress the ambiguous reading.
UNCONFIRMED_RAW = -32640
UNCONFIRMED_FLOAT = struct.unpack(">f", bytes.fromhex("c3a33333"))[0]


def capability_schema(_config: ConnectionConfig) -> CapabilitySet:
    return CapabilitySet(
        "multimeter",
        CHANNELS,
        (
            Capability(
                "input_raw", readable=True, channel_scoped=True, source=SOURCE, section="13.6, 30001–30016"
            ),
        ),
    )


class RecorderAdapter:
    adapter_id = ADAPTER_ID

    def __init__(self, config: ConnectionConfig, transport: RecorderTransport):
        self.config = config
        self.transport = transport
        self.settings = Settings.from_config(config)
        self.identity: DeviceIdentity | None = None
        self.capability_details = {
            "acquisition_channel_selection": "scan_list",
            "acquisition_channels": list(self.settings.channels),
        }

    def raw_inputs(self) -> tuple[int, ...]:
        return struct.unpack(">16h", self.transport.inputs(self.settings.station, 0, 16))

    def identify(self) -> DeviceIdentity:
        raw = self.raw_inputs()
        self.transport.inputs(self.settings.station, 100, 32)
        if not any(option.startswith("channels=") for option in self.config.options):
            active = [ch for ch, value in zip(CHANNELS, raw, strict=True) if value != UNCONFIRMED_RAW]
            self.capability_details["acquisition_channels"] = active or list(CHANNELS)
        self.identity = DeviceIdentity(
            "Pangu",
            "KT616R",
            confidence="model_only",
            raw="Operator-selected KT616R; FC04 input banks 30001–30016 and 30101–30132 responded. No serial readback.",
        )
        return self.identity

    def capabilities(self) -> CapabilitySet:
        return capability_schema(self.config)

    def read_status(self) -> DeviceStatus:
        values = self.raw_inputs()
        return DeviceStatus(
            {
                "registers": values,
                "unconfirmed_channels": [
                    channel
                    for channel, value in zip(CHANNELS, values, strict=True)
                    if value == UNCONFIRMED_RAW
                ],
            }
        )

    def read_settings(self, operations: tuple[str, ...], channel: int = 1) -> tuple[ParameterReading, ...]:
        if channel not in CHANNELS:
            raise ValidationError("Invalid KT616R channel")
        for name in operations:
            self.capabilities().get(name)
        if not operations:
            return ()
        value = self.raw_inputs()[channel - 1]
        return tuple(ParameterReading(name, channel, value, source="instrument") for name in operations)

    def apply(self, command: DeviceCommand) -> ApplyResult:
        raise Unsupported("KT616R integration supports passive input reads only")

    def acquire(self, request: AcquisitionRequest) -> MeasurementBatch:
        request.validate()
        if not self.identity:
            raise DeviceError("Identify before acquisition", "identity_required")
        if request.kind != "scalar" or request.initiate:
            raise Unsupported("KT616R supports passive scalar acquisition")
        if not set(request.channels) <= set(self.settings.channels):
            raise ValidationError("Select only channels configured for this recorder")
        order = self.settings.order
        raw = self.raw_inputs()
        block = self.transport.inputs(self.settings.station, 100, 32) if order else b""
        measurements = []
        for ch in request.channels:
            unconfirmed = raw[ch - 1] == UNCONFIRMED_RAW
            if order:
                word = block[4 * (ch - 1) : 4 * ch]
                value = struct.unpack(">f", bytes(word[i] for i in ORDERS[order]))[0]
                # Same field observation as int16 0x8080. Preserve the wire value in status/trace.
                unconfirmed = unconfirmed or not math.isfinite(value) or value == UNCONFIRMED_FLOAT
                unit = self.settings.unit
                quantity = "temperature" if unit in {"°C", "°F", "K"} else "process_value"
            else:
                value = raw[ch - 1]
                unit, quantity = "count", "input_raw"
            if unconfirmed:
                raise DeviceError(
                    f"CH{ch} 读数未确认（现场空载特征或非有限值），本帧不参与监测和记录",
                    "acquisition_setup_blocked",
                )
            measurements.append(Measurement(quantity, unit, (value,), ch))
        return MeasurementBatch(
            self.identity,
            tuple(measurements),
            {
                "station": self.settings.station,
                "channels": request.channels,
                "format": order or "signed_int16",
                "unit": unit,
            },
            validation="full",
        )

    def close(self) -> None:
        self.transport.close()
