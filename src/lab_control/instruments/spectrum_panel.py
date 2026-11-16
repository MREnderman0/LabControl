"""Attach optional panel-scale readback to a validated spectrum acquisition."""

import time
from dataclasses import replace

from lab_control.domain.instruments import DeviceError
from lab_control.instruments.base import ScpiAdapter, number


class SpectrumPanelAdapter(ScpiAdapter):
    _display_until = 0.0
    _display_key = None
    _display_scale = None

    def invalidate_acquisition_context(self, reason):
        self._display_until = 0.0

    def read_panel_scale(self, config, unit):
        raise NotImplementedError

    def read_scale_mode(self, command):
        raw = self.transport.query(command).decode("ascii").strip().strip('"').upper()
        if raw in {"LOG", "LOGARITHMIC"}:
            return "log"
        if raw in {"LIN", "LINE", "LINEAR"}:
            return "linear"
        raise ValueError(f"Unknown scale mode: {raw!r}")

    def scale_value(self, command):
        return number(self.transport.query(command))

    def acquire(self, request):
        if not request.spectrum_display:
            return self.acquire_trace(request)

        request.validate()
        # Refresh panel settings in the same serial worker as the trace. Between
        # snapshots DSA815/FPH keep their existing one-query cached path.
        now = time.monotonic()
        due = now >= self._display_until
        context = getattr(self, "_acquisition_context", None)
        if due and context and request.consistency in {"live", "stream"}:
            # Most settings are unchanged between panel polls. Only invalidate the
            # qualified trace snapshot when the physical axis or amplitude changes.
            fields = (
                "start_frequency",
                "stop_frequency",
                "amplitude_unit",
                "reference_level",
                "reference_offset",
            )
            if any(self.read(field) != context[field] for field in fields):
                self.invalidate_acquisition_context("panel_scale_refresh")
        batch = self.acquire_trace(request)
        (trace,) = batch.measurements
        key = (
            request.context_id,
            trace.unit,
            batch.configuration.get("reference_level"),
            batch.configuration.get("reference_offset"),
            batch.configuration.get("amplitude_unit"),
        )
        if (
            due
            or key != self._display_key
            or (batch.validation != "cached" and getattr(self, "supports_live_acquisition", False))
        ):
            try:
                scale = self.read_panel_scale(batch.configuration, trace.unit)
            except (DeviceError, ValueError, OverflowError, UnicodeError):
                # The trace has already passed independent validation. A missing
                # optional display parameter must not discard it or retain a stale scale.
                scale = None
            self._display_scale = scale
            self._display_key = key
            self._display_until = time.monotonic() + (2.0 if scale is not None else 5.0)
        if self._display_scale is None:
            return batch
        trace = replace(trace, metadata={**trace.metadata, "spectrum_display": self._display_scale})
        return replace(batch, measurements=(trace,))

    def acquire_trace(self, request):
        return super().acquire(request)
