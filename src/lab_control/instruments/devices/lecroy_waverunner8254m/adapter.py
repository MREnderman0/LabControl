"""WaveRunner 8254M analog controls, native Single and displayed-waveform acquisition."""

from dataclasses import replace
from typing import Any

from lab_control.domain.instruments import (
    AcquisitionRequest,
    DeviceError,
    MeasurementBatch,
    Unsupported,
    utc_now,
)
from lab_control.domain.oscilloscope import ScopeDisplay
from lab_control.instruments.common.lecroy_waveform import waveform
from lab_control.instruments.scpi_single import LecroySingle

from .control import PanelControl

# The shared decoder accepts up to ten million samples per channel (BYTE or WORD).
MAX_REPLY_BYTES = 20_000_000 + 65536


class WaveRunner8254MAdapter(LecroySingle, PanelControl):
    def single_count(self, channels):
        if self.identity is None:
            raise DeviceError("Identify the scope before Single", "identity_required")
        if any(ch not in self.profile.channels for ch in channels):
            raise Unsupported("Single requires analog channels C1–C4")
        if any(self.read("channel_enabled", ch) != "ON" for ch in channels):
            raise Unsupported("Selected Single channel is disabled on the scope")
        return super().single_count(channels)

    def _snapshot(self, channels: tuple[int, ...]) -> dict[str, Any]:
        return {
            f"{ch}.{name}": self.read(name, ch)
            for name, command in self.profile.commands.items()
            if command.capability.readable and name not in {"trigger_source"}
            for ch in (channels if command.capability.channel_scoped else (1,))
        }

    def acquire(self, request: AcquisitionRequest) -> MeasurementBatch:
        request.validate()
        if self.identity is None:
            raise DeviceError("Identify the scope before acquisition", "identity_required")
        if (
            request.kind != "waveform"
            or request.initiate
            or request.waveform_extent == "memory"
            or any(ch not in self.profile.channels for ch in request.channels)
        ):
            raise Unsupported("Use the Single action to arm, or read displayed C1–C4 waveforms")
        before = self._snapshot(request.channels)
        if any(before[f"{ch}.channel_enabled"] != "ON" for ch in request.channels):
            raise Unsupported("Selected waveform channel is disabled on the scope")
        fmt = before["1.transfer_format"]
        setup = before["1.waveform_setup"]
        if fmt not in ("DEF9,BYTE,BIN", "DEF9,WORD,BIN"):
            raise Unsupported("Select a binary BYTE or WORD waveform transfer on the scope")
        if setup["SP"] not in (0, 1) or any(setup[k] for k in ("FP", "NP", "SN")):
            raise Unsupported("Full waveforms require an unsparsed, unrestricted waveform transfer")

        values = []
        for ch in request.channels:
            query = f"C{ch}:WF? ALL"
            bounded = getattr(self.transport, "query_binary", None)
            raw = bounded(query, MAX_REPLY_BYTES) if callable(bounded) else self.transport.query(query)
            if len(raw) > MAX_REPLY_BYTES:
                raise DeviceError("Waveform exceeds receive budget", "parse_error")
            m = waveform(raw, ch)
            if m.unit != "V":
                raise Unsupported(f"Only voltage waveforms are qualified, received {m.unit!r}")
            if (
                m.metadata["sample_width"] != (2 if "WORD" in fmt else 1)
                or m.metadata["byte_order"] != before["1.byte_order"]
            ):
                raise DeviceError("Waveform transfer format changed", "configuration_changed")
            display = ScopeDisplay(
                before[f"{ch}.scale"],
                -before[f"{ch}.offset"],
                before["1.timebase"],
                -before["1.time_position"],
                m.unit,
                vertical_offset_sign=-1,
                horizontal_position_sign=-1,
            )
            values.append(
                replace(
                    m, metadata={**m.metadata, "scope_display": display.metadata(), "received_at": utc_now()}
                )
            )
        after = self._snapshot(request.channels)
        if before != after:
            raise DeviceError("Scope settings changed during waveform transfer", "configuration_changed")
        return MeasurementBatch(
            self.identity,
            tuple(values),
            after,
            validation="full",
            settings_timestamps=dict.fromkeys(after, utc_now()),
            performance={"configuration_source": "instrument_before_and_after", "sequential_channels": True},
        )
