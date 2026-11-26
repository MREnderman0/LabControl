"""Attach to a provisioned 4-slot platform; never deploy or reset slots implicitly."""

import re

import numpy as np

from lab_control.domain.instruments import (
    ApplyResult,
    Capability,
    CapabilitySet,
    DeviceError,
    DeviceStatus,
    Measurement,
    MeasurementBatch,
    Parameter,
    ParameterReading,
    Quality,
    Unsupported,
    ValidationError,
)

from .capture_settings import ACK_ONLY, CAPTURE, apply_capture, capture_capabilities, wire_parameters
from .protocol import ADAPTER, CHANNELS, FOUR_SLOT_SESSION, Rest

ROLES = {
    "signal_generator": "waveformgenerator",
    "oscilloscope": "oscilloscope",
    "spectrum_analyzer": "spectrumanalyzer",
}
KINDS = {"waveform": "oscilloscope", "spectrum": "spectrum_analyzer"}
LABELS = {"signal_generator": "信号源", "oscilloscope": "示波器", "spectrum_analyzer": "频谱仪"}
FRONTEND = {
    "impedance": ("50Ohm", "1MOhm"),
    "coupling": ("DC", "AC"),
    "attenuation": ("0dB", "20dB", "40dB"),
    "bandwidth": ("300MHz", "600MHz"),
}


def schema(config):
    return CapabilitySet(
        "oscilloscope",
        CHANNELS,
        (
            Capability(
                "connect_generator_channel", writable=True, requires_confirmation=True, channel_scoped=True
            ),
            Capability(
                "shared_frontend",
                {
                    key: Parameter("enum", choices=tuple(v.upper() for v in options), required=False)
                    for key, options in FRONTEND.items()
                },
                writable=True,
                readable=True,
                requires_confirmation=True,
                channel_scoped=True,
                partial_update="native",
            ),
            Capability("attach_workspace", writable=True, requires_confirmation=True),
            *capture_capabilities(),
            Capability(
                "standard_waveform",
                {
                    "frequency": Parameter(unit="Hz", minimum=1, maximum=1e6),
                    "amplitude": Parameter(unit="Vpp", minimum=0.004, maximum=0.1),
                },
                writable=True,
                requires_confirmation=True,
                channel_scoped=True,
            ),
            Capability("waveform_stop", writable=True, requires_confirmation=True, channel_scoped=True),
            Capability(
                "select_measurement_input",
                {"role": Parameter("enum", choices=("WAVEFORM", "SPECTRUM"))},
                writable=True,
                requires_confirmation=True,
                channel_scoped=True,
            ),
        ),
    )


def slot_map(names):
    if not isinstance(names, list) or len(names) != 4 or not all(isinstance(n, str) for n in names):
        raise Unsupported("需要既有四槽模式；不会自动部署或覆盖现有配置")
    normalized = [n.replace(" ", "").lower() for n in names[:3]]
    if set(normalized) != set(ROLES.values()):
        raise Unsupported("前三槽须分别装有信号源、示波器和频谱仪（顺序不限）；不改动第四槽")
    return {role: normalized.index(group) + 1 for role, group in ROLES.items()}


def routes(rows):
    if not isinstance(rows, list):
        raise DeviceError("路由回包格式无效")
    result = {}
    for row in rows:
        dest, src = row["destination"], row["source"]
        if dest in result or not all(isinstance(x, str) for x in (dest, src)):
            raise DeviceError("路由不明确，停止操作")
        result[dest] = src
    return result


class Adapter:
    adapter_id = ADAPTER

    def __init__(self, config, api=None):
        self.config = config
        self.api = api or Rest(config.resource, config.timeout, keep_alive=True)
        try:
            self.identity = self.api.identity(config.expected_serial)
        except Exception:
            self.api.close()
            raise
        self.slots: dict[str, int] = {}
        self.output_receipts: dict[str, dict] = {}

    def identify(self):
        return self.identity

    def capabilities(self):
        return schema(self.config)

    def read_settings(self, operations, channel=1):
        readable = {op for op, (method, _) in CAPTURE.items() if method not in ACK_ONLY}
        requested = [
            op for op in operations if op in readable or op in {"shared_frontend", "scope_samplerate"}
        ]
        if not requested:
            return ()
        if channel not in CHANNELS:
            raise ValidationError("无效的物理输入通道")
        if not self.api.key:
            return tuple(
                ParameterReading(op, channel, quality=Quality.STALE, error="等待连接已有仪器槽")
                for op in requested
            )
        self._attach()
        readings = []
        for op in requested:
            if op == "shared_frontend":
                data = self._frontend(channel)
            else:
                role = "oscilloscope" if op.startswith("scope_") else "spectrum_analyzer"
                method = "samplerate" if op == "scope_samplerate" else CAPTURE[op][0]
                data = self._slot(role, "GET", "get_" + method)
                if not isinstance(data, dict) or not data:
                    raise DeviceError("采集参数读回无效")
            readings.append(ParameterReading(op, channel, data, "instrument"))
        return tuple(readings)

    @property
    def capability_details(self):
        return {
            "workspace_attached": bool(self.api.key and self.slots),
            "output_receipts": self.output_receipts.copy(),
        }

    def read_status(self):
        # No ownership claim. While recording locks writes, another role may join
        # only an already verified input; checking this must not disturb the recorder.
        values = {"model": self.identity.model, "attached": self.capability_details["workspace_attached"]}
        if values["attached"]:
            self._attach()
            mapping = self._routes()
            values["measurement_inputs"] = {
                kind: list(self._inputs(role, mapping)) for kind, role in KINDS.items()
            }
        return DeviceStatus(values)

    def _attach(self, *, confirmed=False):
        # Only a confirmed user action may claim; discovery and sampling never deploy or claim.
        newly_claimed = not self.api.key
        if newly_claimed:
            if not confirmed:
                raise DeviceError(
                    "尚未连接已有四槽，请从仪器面板开始监测或开启输出", "acquisition_setup_blocked"
                )
            self.api.call(
                "POST",
                "moku/claim_ownership",
                dict(
                    force_connect=False,
                    force_deploy=False,
                    ignore_busy=False,
                    persist_state=True,
                    client_name="LabControl",
                ),
            )
        try:
            if newly_claimed and FOUR_SLOT_SESSION in self.config.options:
                # MokuOS 4.4 starts each client outside MiM, even with persisted state.
                # Only explicitly commissioned assets may select this platform.
                self.api.call("GET", "moku/platform/4")
            self.slots = slot_map(self.api.call("GET", "mim/get_instruments"))
        except Exception as exc:
            self.slots = {}
            if newly_claimed:
                try:
                    self._release_ownership()
                except Exception as cleanup:
                    exc.add_note(f"Could not confirm ownership release: {cleanup}")
                finally:
                    self.close_local()
            raise

    def _slot(self, role, method, operation, params=None):
        return self.api.call(method, f"slot{self.slots[role]}/{ROLES[role]}/{operation}", params)

    def _routes(self):
        return routes(self.api.call("GET", "mim/get_connections"))

    def _frontend(self, ch):
        read = self.api.call("POST", "mim/get_frontend", {"channel": ch})
        if not isinstance(read, dict) or not read:
            raise DeviceError("模拟前端读回无效")
        if "attenuation" not in read and read.get("gain") in ("0dB", "-20dB", "-40dB"):
            read = {**read, "attenuation": read["gain"].lstrip("-")}
        return read

    def _inputs(self, role, mapping):
        slot = self.slots[role]
        sources = self._slot(role, "GET", "get_sources") if role == "oscilloscope" else None
        if role == "oscilloscope" and not isinstance(sources, list):
            raise DeviceError("示波器通道来源回包无效，停止接线和采样", "parse_error")
        inputs: dict[int, int] = {}
        for index, letter in enumerate("ABCD", 1):
            src = mapping.get(f"Slot{slot}In{letter}", "")
            if not re.fullmatch(r"Input[1-4]", src):
                continue
            ch = int(src[-1])
            if sources is None:
                inputs.setdefault(ch, index)
            else:
                for row in sources:
                    if (
                        row["enabled"] is True
                        and row["source"] in (f"Input{index}", f"GenericInput{letter}")
                        and row["channel"] in CHANNELS
                    ):
                        inputs.setdefault(ch, row["channel"])
        return inputs

    def _input(self, role, ch, mapping):
        inputs = self._inputs(role, mapping)
        if ch in inputs:
            return inputs[ch]
        raise DeviceError(f"{LABELS[role]} ch{ch} 没有对应输入路由", "acquisition_setup_blocked")

    def _snapshot(self, role, ch):
        logical = self._input(role, ch, self._routes())
        frontend = self._frontend(ch)
        getters = (
            ("get_timebase", "get_samplerate", "get_acquisition_mode", "get_interpolation")
            if role == "oscilloscope"
            else ("get_span", "get_rbw", "get_window")
        )
        settings = {k.removeprefix("get_"): self._slot(role, "GET", k) for k in getters}
        if not all(isinstance(v, dict) and v for v in settings.values()):
            raise DeviceError("仪器配置回包不完整")
        if not all(isinstance(obj, dict) and obj for obj in (frontend, settings)):
            raise DeviceError("采集参数回读不完整")
        return logical, frontend, settings

    def acquire(self, request):
        request.validate()
        if (
            request.kind not in KINDS
            or len(request.channels) != 1
            or request.channels[0] not in CHANNELS
            or request.initiate
            or request.waveform_extent == "memory"
        ):
            raise Unsupported("每面板一次选择一个物理通道，仅读取已部署仪器的完整帧")
        role, ch = KINDS[request.kind], request.channels[0]
        self._attach()
        before = self._snapshot(role, ch)
        params = dict(wait_reacquire=True, wait_complete=True, timeout=max(0.1, self.config.timeout * 0.7))
        spectrum = request.kind == "spectrum"
        if spectrum:
            params.update(units="dBm", psdUnits=False, strict=True)
        frame = self._slot(role, "POST", "get_data", params)
        # Malformed data is a parse failure, not a transient settings change.
        try:
            x, y = (np.asarray(frame[k]) for k in ("frequency" if spectrum else "time", f"ch{before[0]}"))
            if (
                not all(
                    a.dtype.kind in "ifu"
                    and a.ndim == 1
                    and 2 <= a.size <= 4_000_000
                    and np.isfinite(a).all()
                    for a in (x, y)
                )
                or x.shape != y.shape
                or not (x[1:] > x[:-1]).all()
            ):
                raise ValueError("axis or trace invalid")
        except (KeyError, TypeError, ValueError) as exc:
            raise DeviceError("无效帧，未显示或保存", "parse_error") from exc
        if self._snapshot(role, ch) != before:
            raise DeviceError("采集中路由或输入参数改变", "configuration_changed")
        label = f"{LABELS[role]} ch{ch}"
        measurement = Measurement(
            "spectrum" if spectrum else "voltage",
            "dBm" if spectrum else "V",
            y,
            ch,
            x,
            "Hz" if spectrum else "s",
            metadata={"channel_label": label},
        )
        return MeasurementBatch(
            self.identity,
            (measurement,),
            {"channel": label, "frontend": before[1], "settings": before[2]},
            validation="full",
        )

    def apply(self, command):
        ch, op = command.channel, command.operation
        if type(ch) is not int or ch not in CHANNELS or not command.confirmed:
            raise ValidationError("选择有效通道并明确确认操作")
        cap = self.capabilities().get(op)
        if not cap.writable:
            raise Unsupported("此参数只支持读回")
        params = cap.validate(command.parameters)
        if op == "shared_frontend" and not params:
            raise ValidationError("请选择要修改的模拟前端参数")
        if op in CAPTURE:
            body = wire_parameters(op, params)
            self._attach(confirmed=True)
            return apply_capture(self, op, params, body, ch)
        self._attach(confirmed=True)
        if op == "attach_workspace":
            return ApplyResult("success", op, params, {"slots": self.slots}, acknowledged=True)
        mapping = self._routes()
        if op == "shared_frontend":
            consumers = [dest for dest, src in mapping.items() if src == f"Input{ch}"]
            allowed = {f"Slot{self.slots[r]}In{letter}" for r in KINDS.values() for letter in "ABCD"}
            if not consumers or not set(consumers) <= allowed:
                raise Unsupported("物理通道未连接到测量槽或同时驱动其他目的端，禁止修改前端")
            baseline = self._frontend(ch)
            target = {}
            for key, options in FRONTEND.items():
                value = {v.upper(): v for v in options}.get(params.get(key), baseline.get(key))
                if value not in options:
                    raise Unsupported(f"无法保留当前模拟前端设置 {key}")
                target[key] = value
            self.api.call("POST", "mim/set_frontend", {"channel": ch, "strict": True, **target})
            read = self._frontend(ch)
            if any(read.get(k) != v for k, v in target.items()):
                raise DeviceError("前端读回与设置不符，最终状态待核实")
            return ApplyResult("success", op, params, read, 1)
        if op == "select_measurement_input":
            role = KINDS[params["role"].lower()]
            # Preserve an existing mapping, including cross-wired scope display sources.
            try:
                self._input(role, ch, mapping)
            except DeviceError as exc:
                if exc.code != "acquisition_setup_blocked":
                    raise
                slot = self.slots[role]
                if any(src.startswith(f"Slot{slot}Out") for src in mapping.values()):
                    raise Unsupported("此测量槽驱动其他目的端，禁止改接")
                if role == "oscilloscope" and not any(
                    row["enabled"] is True and row["source"] in ("Input1", "GenericInputA")
                    for row in self._slot(role, "GET", "get_sources")
                ):
                    raise Unsupported("请在原配置中启用第一路槽输入作为示波器数据源")
                delta = {"destination": f"Slot{slot}InA", "source": f"Input{ch}"}
                previous = mapping.get(delta["destination"])
                if previous is not None and not re.fullmatch(r"Input[1-4]", previous):
                    raise Unsupported("测量槽输入由其他功能驱动，不覆盖已有路由")
                self.api.call("POST", "mim/set_connections", {"connections": [delta]})
                after = self._routes()
                if after != {**mapping, delta["destination"]: delta["source"]}:
                    raise DeviceError("路由读回不符，最终状态待核实")
                self._input(role, ch, after)
            return ApplyResult("success", op, params, {"channel": f"{LABELS[role]} ch{ch}"}, 1)
        role = "signal_generator"
        slot = self.slots[role]
        dest = f"Output{ch}"
        src = mapping.get(dest)
        if src is None and op != "waveform_stop":
            summary = self._slot(role, "GET", "summary")
            rows = re.findall(r"^Output ([1-4A-D]) \(([^)]+)\)", summary, re.MULTILINE | re.IGNORECASE)
            outputs = [
                (letter.upper() if letter.isalpha() else "ABCD"[int(letter) - 1], state)
                for letter, state in rows
            ]
            for n, state in outputs:
                port = f"Slot{slot}Out{n}"
                if (
                    state.lower() == "off"
                    and sum(k == n for k, _ in outputs) == 1
                    and port not in mapping.values()
                ):
                    src = port
                    break
            if src is None:
                raise Unsupported("无已核实关闭且空闲的信号源通道，不改接")
        match = re.fullmatch(rf"Slot{slot}Out([A-D])", src or "")
        if not match or (dest in mapping and list(mapping.values()).count(src) != 1):
            raise Unsupported(f"信号源 ch{ch} 无独占路由，不覆盖其他功能")
        logical = "ABCD".index(match[1]) + 1
        body = {"channel": logical, "strict": True, "type": "Off"}
        if op == "standard_waveform":
            if (
                self.api.call("POST", "mim/get_output", {"channel": ch}).get("output_gain") != "0dB"
                or self._slot(role, "POST", "get_output_termination", {"channel": logical}).get("termination")
                != "50Ohm"
            ):
                raise ValidationError("只允许已核实 0 dB 增益及 50 Ω 终端设置的输出")
            body.update(type="Sine", offset=0, **params)
        if dest not in mapping:
            self.output_receipts.pop(str(ch), None)
            self.api.call(
                "POST", "mim/set_connections", {"connections": [{"destination": dest, "source": src}]}
            )
            mapping = {**mapping, dest: src}
            if self._routes() != mapping:
                raise DeviceError("信号源路由读回不符，未发送波形命令；请核对路由")
        if op == "connect_generator_channel":
            return ApplyResult("success", op, params, {"channel": f"信号源 ch{ch}"}, 1)
        # A write or its verification can fail after the instrument changed the output.
        self.output_receipts[str(ch)] = {"state": "uncertain"}
        ack = self._slot(role, "POST", "generate_waveform", body)
        if (
            not isinstance(ack, dict)
            or ack.get("type") != body["type"]
            or any(ack.get(k) != body[k] for k in ("frequency", "amplitude", "offset") if k in body)
            or self._routes() != mapping
        ):
            raise DeviceError("输出回执或路由异常，最终状态待核实；不会重发")
        self.output_receipts[str(ch)] = (
            {"state": "on", "frequency": ack["frequency"], "amplitude": ack["amplitude"]}
            if body["type"] == "Sine"
            else {"state": "off"}
        )
        return ApplyResult("success", op, params, {"channel": f"信号源 ch{ch}"}, 1, acknowledged=True)

    def _release_ownership(self):
        if self.api.key:
            self.api.call("POST", "moku/relinquish_ownership")
            self.api.key = None

    def close(self):
        try:
            self._release_ownership()
        finally:
            self.close_local()

    def close_local(self):
        try:
            self.api.close()
        finally:
            self.api.key = None
            self.slots = {}
            self.output_receipts.clear()
