"""Live CVAR limits and verified panel writes for the WaveRunner 8254M."""

import math
from dataclasses import replace

from lab_control.domain.instruments import DeviceError, Unsupported
from lab_control.instruments.base import ScpiAdapter
from lab_control.instruments.common.lecroy_ranges import (
    MEMORY_QUERIES,
    PATHS,
    cvar_query,
    fixed_rate_timebase,
    plausible_adaptation,
    read_range,
)
from lab_control.instruments.common.lecroy_waveform import numeric, trigger_setup, value_text

EDGE_CONTROLS = {"trigger_source", "trigger_level", "trigger_slope"}
MODES = {"run": "AUTO", "stop": "STOP", "single": "SINGLE"}


class PanelControl(ScpiAdapter):
    def _range(self, name, ch):
        spec = self.profile.commands[name].capability.parameters["value"]
        bounds = read_range(self.transport, name, ch, spec)
        if (
            name == "timebase"
            and value_text(self.transport.query(MEMORY_QUERIES["mode"])) == "FixedSampleRate"
        ):
            bounds = fixed_rate_timebase(bounds, self.read("sample_rate"))
        return bounds

    def _trigger(self):
        raw = self.transport.query("TRSE?")
        kind = value_text(raw).partition(",")[0].upper()
        if kind != "EDGE":
            self._edge = False
            return {"type": kind}
        setup = trigger_setup(raw)
        self._edge = setup["SR"] in {"C1", "C2", "C3", "C4"}
        return setup

    def refresh_capabilities(self):
        self._ranges = {}
        try:
            self._trigger()
            self._ranges = {
                (ch, name): self._range(name, ch)
                for name in PATHS
                for ch in (
                    self.profile.channels if self.profile.commands[name].capability.channel_scoped else (1,)
                )
            }
            self.capability_details["memory"] = {
                "mode": value_text(self.transport.query(MEMORY_QUERIES["mode"])),
                "points": numeric(self.transport.query(MEMORY_QUERIES["points"])),
                "sample_rate": self.read("sample_rate"),
            }
            self.capability_error = ""
        except Exception as exc:
            self.capability_error = str(exc)
            raise

    def capabilities_for_channel(self, channel):
        schema = super().capabilities_for_channel(channel)
        ops = []
        for cap in schema.operations:
            if cap.name in PATHS:
                bounds = getattr(self, "_ranges", {}).get((channel if cap.channel_scoped else 1, cap.name))
                if bounds is None:
                    cap = replace(cap, writable=False, reason="请先刷新仪器参数范围")
                else:
                    cap = replace(cap, parameters={"value": bounds})
            if cap.name in EDGE_CONTROLS and not getattr(self, "_edge", False):
                cap = replace(cap, writable=False, reason="仅支持模拟通道的边沿触发")
            ops.append(cap)
        return replace(schema, operations=tuple(ops))

    def preflight(self, command, parameters):
        if self.identity is None:
            raise DeviceError("Identify the scope before control", "identity_required")
        name = command.operation
        if name == "single" and self.read("trigger_mode") == "SINGLE":
            raise DeviceError("A Single is already armed", "task_busy")
        if name in EDGE_CONTROLS:
            self._before_trigger = self._trigger()
            if not self._edge:
                raise Unsupported("Only analog edge trigger controls are qualified")
        if name in PATHS:
            self._write_range = self._range(name, command.channel)
            self._write_range.validate(name, parameters["value"])

    def encode(self, operation, spec, context, parameters):
        if operation == "bandwidth_limit":
            # MAUI automation enum strings use the instrument's spelling.
            label = {"FULL": "Full", "20MHZ": "20MHz", "200MHZ": "200MHz"}[parameters["value"]]
            context = {**context, "value": label}
        return super().encode(operation, spec, context, parameters)

    def complete_setting(self, command):
        if command.operation in MODES or command.operation == "trigger_mode":
            return
        if numeric(self.transport.query("*OPC?")) != 1:
            raise DeviceError("Scope did not confirm the write", "configuration_uncertain")

    def adapted_readback(self, command, parameters, readback):
        op, ch = command.operation, command.channel
        requested = parameters["value"]
        if op not in PATHS or not plausible_adaptation(op, requested, readback, self._write_range):
            return {}
        evidence = [
            numeric(self.transport.query(cvar_query(op, ch, key)))
            for key in ("GetRequestedValue", "GetAdaptedValue")
        ]
        if math.isclose(evidence[0], requested, rel_tol=1e-6, abs_tol=1e-15) and math.isclose(
            evidence[1], readback, rel_tol=1e-6, abs_tol=1e-15
        ):
            return {"source": "instrument_cvar", "requested": evidence[0], "adapted": evidence[1]}
        return {}

    def apply(self, command):
        name = command.operation
        if name == "trigger_mode" and str(command.parameters.get("value")).upper() == "SINGLE":
            self.validate_command(command)
            result = self.apply(replace(command, operation="single", parameters={}))
            return replace(result, operation=name, requested={"value": "SINGLE"})
        result = super().apply(command)
        if result.outcome == "success":
            result = self._confirm_readback(name, result)
        if result.outcome != "success":
            self.configuration.pop(f"{command.channel}.{name}", None)
        return result

    def _confirm_readback(self, name, result):
        if name in MODES:
            if result.readback != MODES[name] and not (name == "single" and result.readback == "STOP"):
                return replace(result, outcome="rejected", error="Trigger state differs from request")
        elif name == "trigger_source":
            try:
                after = self._trigger()
            except DeviceError as exc:
                return replace(result, outcome="partial", error=str(exc))
            if after != {**self._before_trigger, "SR": result.requested["value"]}:
                return replace(result, outcome="rejected", error="Trigger qualifiers changed")
        elif isinstance(result.requested.get("value"), str) and result.requested["value"] != result.readback:
            return replace(result, outcome="rejected", error="Instrument returned a different setting")
        elif (
            name in PATHS
            and not result.adaptation
            and not math.isclose(result.requested["value"], result.readback, rel_tol=1e-6, abs_tol=1e-15)
        ):
            return replace(result, outcome="rejected", error="Readback differs at instrument precision")
        return result
