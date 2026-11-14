"""DH-8318 passive measurements and verified single-channel settings."""

from __future__ import annotations

import math
import re

from lab_control.domain.instruments import (
    AcquisitionRequest,
    ApplyResult,
    DeviceError,
    DeviceIdentity,
    DeviceStatus,
    Measurement,
    ParameterReading,
    Quality,
    Unsupported,
    ValidationError,
    readable_serial,
)
from lab_control.instruments.base import Profile, ScpiAdapter, identity_from_scpi, number, read_command
from lab_control.transport.streams import SerialTransport

from .controls import MODES, NUMERIC, SETPOINTS, control_commands, decode_setting

CHANNELS = tuple(range(1, 9))
READINGS = {
    "voltage": ("MEAS:VOLT?", "V"),
    "current": ("MEAS:CURR?", "A"),
}


def dh8318() -> Profile:
    return Profile(
        "duoheyi.dh8318",
        ("DUOHEYI",),
        ("DH-8318",),
        "electronic_load",
        CHANNELS,
        "《单通道、多通道 可编程直流电子负载 用户使用手册》",
        "V1.1，第 7、19、22–28 页",
        {
            **control_commands(),
            **{f"measured_{q}": read_command(f"measured_{q}", query) for q, (query, _) in READINGS.items()},
        },
        error_query="",
        acquisition="power",
    )


def create_transport(config, runtime):
    if config.transport != "serial":
        raise Unsupported("DH-8318 需要 RS232 串口")
    if config.terminator != "\n":
        raise ValidationError("DH-8318 串口结束符须为 LF")
    return SerialTransport(config.resource, config.baud_rate, config.timeout, b"\n")


def line(raw: bytes) -> bytes:
    if len(raw) > 512 or not raw.endswith(b"\n"):
        raise DeviceError("DH-8318 串口回包不完整", "parse_error")
    body = raw.removesuffix(b"\n").removesuffix(b"\r").strip(b" ")
    if not body or any(c < 32 or c > 126 for c in body):
        raise DeviceError("DH-8318 串口回包不是单行 ASCII", "parse_error")
    return body


class Dh8318Adapter(ScpiAdapter):
    control_desynchronized = False
    selected_channels = CHANNELS

    def decode_identity(self, raw: bytes) -> DeviceIdentity:
        body = line(raw)
        if len(body.split(b",")) != 4:
            raise DeviceError("DH-8318 身份回包字段不完整", "parse_error")
        identity = identity_from_scpi(body)
        if identity.manufacturer.upper() != "DUOHEYI" or not self.profile.matches_identity(identity):
            raise DeviceError("所连设备的厂家或型号与 DH-8318 不符", "identity_conflict")
        if not readable_serial(identity.serial) or not identity.firmware:
            raise DeviceError("DH-8318 身份回包缺少序列号或固件", "parse_error")
        return identity

    def identify(self):
        identity = super().identify()
        self.refresh_capabilities()
        return identity

    def _selection(self) -> tuple[int, ...]:
        selected = line(self.transport.query("CH?")).upper()
        if selected == b"ALL":
            return CHANNELS
        if re.fullmatch(rb"[1-8]", selected):
            return (int(selected),)
        raise DeviceError("DH-8318 返回了无法识别的通道号", "parse_error")

    def refresh_capabilities(self) -> None:
        self._remember_selection(self._selection())

    def _remember_selection(self, selected):
        self.selected_channels = selected
        self.capability_details.update(
            acquisition_channel_selection="scan_list", acquisition_channels=selected
        )

    @property
    def capability_revision(self):
        return self.selected_channels

    def read_status(self) -> DeviceStatus:
        self.refresh_capabilities()
        return DeviceStatus({"selected_channels": self.selected_channels})

    def _read_values(self, channels: tuple[int, ...], quantities: tuple[str, ...]):
        selected = self._selection()
        self._remember_selection(selected)
        if not set(channels).issubset(selected):
            raise DeviceError(f"DH-8318 当前可读通道为 {selected}，请重新选择通道", "configuration_changed")
        messages = {q: line(self.transport.query(READINGS[q][0])) for q in quantities}
        after = self._selection()
        if selected != after:
            self._remember_selection(after)
            raise DeviceError("读取期间 DH-8318 的通道选择发生变化，请重新读取", "configuration_changed")
        readings = {}
        for q, body in messages.items():
            fields = [part.strip(b" ") for part in body.split(b",")]
            if len(fields) != len(selected) or not all(NUMERIC.fullmatch(part) for part in fields):
                raise DeviceError(f"DH-8318 {q} 回包的通道数或数字格式有误", "parse_error")
            readings[q] = dict(zip(selected, (number(part) for part in fields), strict=True))
        return readings

    def read(self, operation: str, channel: int = 1):
        self.capabilities().get(operation)
        if type(channel) is not int or channel not in CHANNELS:
            raise ValidationError("DH-8318 的通道号为 1–8")
        if not operation.startswith("measured_"):
            selected = self._selection()
            if channel not in selected:
                raise DeviceError(f"当前只读回 CH{selected[0]}，请核对面板通道选择", "configuration_changed")
            fields = line(self.transport.query(self.profile.commands[operation].query)).split(b",")
            if len(fields) != len(selected) or self._selection() != selected:
                raise DeviceError("设置读回的通道数或通道选择发生变化", "configuration_changed")
            return decode_setting(operation, fields[selected.index(channel)].strip())
        q = operation.removeprefix("measured_")
        return self._read_values((channel,), (q,))[q][channel]

    def read_settings(self, operations, channel=1):
        self._context(channel, {})
        selected = self._selection()
        self._remember_selection(selected)
        if channel not in selected:
            # Opening a control page is passive. CH? can expose only one channel.
            for op in operations:
                self.capabilities().get(op)
            return tuple(
                ParameterReading(op, channel, None, "unavailable", quality=Quality.STALE) for op in operations
            )
        return super().read_settings(operations, channel)

    def _checked_write(self, payload, query):
        # The panel can enable/disable OK/ERR. A following query is always
        # answered; consume the complete transaction in either configuration.
        try:
            self.transport.write(payload)
            reply = line(self.transport.query(query))
            if reply not in (b"OK", b"ERR"):
                return reply
            value = line(self.transport.read())
            if value in (b"OK", b"ERR"):
                raise DeviceError("设置回包顺序异常", "parse_error")
        except DeviceError as exc:
            self.control_desynchronized = True
            self.transport.close()
            raise DeviceError("串口回包未能核对，请重连并核对通道和负载状态", "disconnected") from exc
        if reply == b"ERR":
            raise DeviceError(f"DH-8318 拒绝设置 {payload}", "command_rejected")
        return value

    def _select_for_write(self, selected):
        token = "ALL" if len(selected) > 1 else str(selected[0])
        if self._checked_write(f"CH {token}", "CH?").upper() != token.encode("ascii"):
            raise DeviceError(f"DH-8318 未确认选择通道 {token}", "configuration_changed")

    def _check_setpoint(self, op, value):
        mode = decode_setting("load_mode", line(self.transport.query("MODE?")))
        required, path, param = SETPOINTS[op]
        if mode != required:
            raise ValidationError(f"当前模式为 {mode}，请先选择 {required} 模式")
        if op in ("current", "voltage"):
            rng = line(self.transport.query(path + ":RANG?"))
            if rng not in (b"0", b"1"):
                raise DeviceError("无法核对通道当前量程", "parse_error")
            low_max = 3 if op == "current" else 15
            if rng == b"0" and value > low_max:
                raise ValidationError(f"目标值超过当前低量程上限 {low_max} {param.unit}")

    def apply(self, request):
        spec, params = self.validate_command(request)
        if self.identity is None or self.control_desynchronized:
            raise DeviceError("请重新连接并核对 DH-8318 身份", "identity_required")
        original = self._selection()
        target = (request.channel,)
        op = request.operation
        value = params["value"]
        attempted = False
        try:
            if original != target:
                self._select_for_write(target)
            if op in SETPOINTS:
                self._check_setpoint(op, value)
            if self._selection() != target:
                raise DeviceError("负载通道已变化，请重新选择", "configuration_changed")
            if op == "load_mode":
                arg = MODES[value]
            elif op == "load_input":
                arg = str(int(value))
            else:
                arg = f"{value:.12g}"
            attempted = True
            actual = decode_setting(op, self._checked_write(f"{spec.query[:-1]} {arg}", spec.query))
            if self._selection() != target:
                raise DeviceError("写入期间通道发生变化，请核对负载状态", "configuration_changed")
            matches = (
                math.isclose(value, actual, rel_tol=1e-5, abs_tol=1e-9)
                if op in SETPOINTS
                else actual == value
            )
            if not matches:
                return ApplyResult("rejected", op, params, actual, 1, "仪器读回值与请求不符")
            self.configuration[f"{request.channel}.{op}"] = params
            return ApplyResult("success", op, params, actual, 1)
        except DeviceError as exc:
            if not attempted:
                raise
            return ApplyResult(
                "rejected" if exc.code == "command_rejected" else "uncertain", op, params, error=str(exc)
            )
        finally:
            if original != target and not self.control_desynchronized:
                try:
                    self._select_for_write(original)
                except DeviceError as exc:
                    raise DeviceError(
                        f"未能恢复原通道选择，请核对面板及拉载状态：{exc}", "restore_failed"
                    ) from exc

    def acquire_measurements(self, request: AcquisitionRequest):
        if request.kind != "scalar" or request.initiate or request.quantity not in ("", *READINGS):
            raise Unsupported("DH-8318 支持只读采集电压和电流")
        quantities = (request.quantity,) if request.quantity else tuple(READINGS)
        values = self._read_values(request.channels, quantities)
        return tuple(
            Measurement(q, READINGS[q][1], [values[q][ch]], ch) for ch in request.channels for q in quantities
        )
