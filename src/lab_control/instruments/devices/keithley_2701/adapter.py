"""Read the configured 7708 measurement channels without starting a buffer scan."""

from dataclasses import replace
from typing import Any

from lab_control.domain.instruments import (
    AcquisitionRequest,
    DeviceError,
    MeasurementBatch,
    Unsupported,
    ValidationError,
)
from lab_control.instruments.base import Profile, ScpiAdapter, read_command

from .protocol import UNITS, channel_list, tagged_reading

SOURCE = "https://download.tek.com/manual/2701-900-01G_Feb_2016.pdf"
QUERIES = {
    "cards": "*OPT?",
    "scan_list": "ROUT:SCAN?",
    "scan_state": "ROUT:SCAN:LSEL?",
    "trigger_source": "TRIG:SOUR?",
    "continuous": "INIT:CONT?",
    "front_inputs": "SYST:FRSW?",
    "monitor_state": "ROUT:MON:STAT?",
    "buffer_feed": "TRAC:FEED:CONT?",
    "data_format": "FORM:DATA?",
    "elements": "FORM:ELEM?",
    "system_channel": "ROUT:CLOS?",
    "closed_relays": "ROUT:MULT:CLOS?",
}
TAGGED_FORMAT = "READ,UNIT,CHAN"


PUBLIC_STATUS = ("cards", "scan_list", "scan_state", "trigger_source")


def profile():
    return Profile(
        "keithley.2701",
        ("KEITHLEY",),
        ("MODEL 2701", "2701"),
        "multimeter",
        (1,),
        SOURCE,
        "2701 User’s Manual §§2, 3, 7, 14, 15; 7708 measurement inputs",
        {
            name: read_command(name, cmd, scoped=False)
            for name, cmd in QUERIES.items()
            if name in PUBLIC_STATUS
        },
        PUBLIC_STATUS,
        acquisition="scalar",
    )


class Keithley2701Adapter(ScpiAdapter):
    supports_live_acquisition = True
    supports_stream_acquisition = False
    acquisition_revision = "2701-tagged-readings-v1"
    capability_revision = 0

    def read(self, operation: str, channel: int = 1):
        # The shared status reader uses channel 1 for instrument-wide queries.
        return super().read(operation, self.profile.channels[0] if channel == 1 else channel)

    def _query(self, command: str) -> str:
        return self.transport.query(command).decode("ascii").strip().upper()

    def _slots(self, cards: str) -> dict[int, str]:
        slots = cards.split(",")
        if len(slots) != 2:
            raise DeviceError(f"Invalid 2701 option string: {cards}", "parse_error")
        serials = {}
        for slot, card in enumerate(slots, 1):
            if card.strip() == "7708":
                serial = self._query(f"SYST:CARD{slot}:SNUM?")
                if not serial.isdecimal() or not int(serial):
                    raise DeviceError(f"Slot {slot} has no physical 7708 serial number", "not_configured")
                serials[slot] = serial
        return serials

    @staticmethod
    def _channels(slots: dict[int, str], scan: str) -> tuple[int, ...]:
        available = {slot * 100 + i for slot in slots for i in range(1, 41)}
        channels = channel_list(scan)
        if any(ch not in available for ch in channels):
            raise Unsupported("扫描列表含未识别的 7708 测量通道")
        return channels

    def refresh_capabilities(self) -> None:
        channels = self._channels(self._slots(self._query("*OPT?")), self._query("ROUT:SCAN?"))
        if self.capability_details.get("acquisition_channels") != list(channels):
            self.profile = replace(self.profile, channels=channels or (1,))
            self.capability_details.update(
                acquisition_channel_selection="scan_list", acquisition_channels=list(channels)
            )
            self.capability_revision += 1

    def _snapshot(self):
        state: dict[str, Any] = {key: self._query(cmd) for key, cmd in QUERIES.items()}
        state["slot_serials"] = self._slots(state["cards"])
        return state

    def _functions(self, channels):
        clist = ",".join(str(ch) for ch in channels)
        result = self._query(f"FUNC? (@{clist})")
        names = [name.strip().strip('"') for name in result.split(",")]
        if len(names) != len(channels):
            raise DeviceError("Incomplete 2701 channel function list", "parse_error")
        if any(name not in {entry[2] for entry in UNITS.values()} for name in names):
            raise Unsupported(f"Unsupported 7708 measurement function: {result}")
        return dict(zip(channels, names, strict=True))

    def _restore(self, original, routed, changed_format):
        failures = []
        for touched, command, expected in (
            (routed, "ROUT:OPEN:ALL", {"system_channel": "(@)", "closed_relays": "(@)"}),
            (changed_format, "FORM:ELEM " + original["elements"], {"elements": original["elements"]}),
        ):
            if touched:
                try:
                    self.transport.write(command)
                    for key, value in expected.items():
                        if self._query(QUERIES[key]) != value:
                            raise ValueError(f"Unexpected {key} after {command}")
                except Exception as exc:
                    failures.append(str(exc))
        if failures:
            self.capability_error = "；".join(failures)
            raise DeviceError("2701 恢复读回失败：" + self.capability_error, "restoration_failed")

    def acquire(self, request: AcquisitionRequest) -> MeasurementBatch:
        request.validate()
        if request.kind not in {"scalar", "measurement"} or request.consistency == "stream":
            raise Unsupported("2701 supports scalar channel readings")
        if self.identity is None or self.capability_error:
            raise DeviceError("2701 must be identified and ready", "not_ready")
        if not request.confirmed or not request.allow_setup:
            raise DeviceError("采集需要临时切换 7708 测量通道并恢复", "confirmation_required")
        original = self._snapshot()
        configured = self._channels(original["slot_serials"], original["scan_list"])
        channels = request.channels
        if not channels or not set(channels).issubset(configured):
            raise ValidationError("Select measurement inputs from the instrument scan list")
        required = {
            "scan_state": "NONE",
            "trigger_source": "IMM",
            "continuous": "1",
            "front_inputs": "0",
            "monitor_state": "0",
            "buffer_feed": "NEV",
            "data_format": "ASC",
            "system_channel": "(@)",
            "closed_relays": "(@)",
        }
        for key, value in required.items():
            if original[key] != value:
                raise DeviceError(f"2701 {key}={original[key]} (requires {value})", "not_configured")
        elements = original["elements"].split(",")
        if not all(e in {"READ", "UNIT", "RNUM", "TST", "CHAN", "LIM"} for e in elements):
            raise Unsupported("Unrecognized 2701 response format")
        functions = self._functions(channels)
        changed_format = original["elements"] != TAGGED_FORMAT
        measurements = []
        routed = False
        try:
            if changed_format:
                self.transport.write("FORM:ELEM " + TAGGED_FORMAT)
                if set(self._query("FORM:ELEM?").split(",")) != set(TAGGED_FORMAT.split(",")):
                    raise DeviceError("2701 did not enable channel tags", "readback_mismatch")
            for channel in channels:
                routed = True  # A write timeout can still mean the relay switched.
                self.transport.write(f"ROUT:CLOS (@{channel})")
                if channel_list(self._query("ROUT:CLOS?")) != (channel,):
                    raise DeviceError(f"2701 failed to select channel {channel}", "readback_mismatch")
                raw = self._query("DATA:FRESH?")
                measurements.append(tagged_reading(raw, channel, functions[channel]))
        finally:
            self._restore(original, routed, changed_format)
        if self._snapshot() != original or self._functions(channels) != functions:
            raise DeviceError("2701 configuration changed during acquisition", "configuration_changed")
        if len({(m.quantity, m.unit) for m in measurements}) != 1:
            raise Unsupported("同一张趋势图的通道须使用相同物理量和单位")
        return MeasurementBatch(
            self.identity,
            tuple(measurements),
            {**original, "functions": functions},
            validation="full",
        )
