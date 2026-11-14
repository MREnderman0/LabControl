"""Read the current channel selection and mean voltage/current of a DH-8318."""

from __future__ import annotations

import re
from dataclasses import replace

from lab_control.domain.instruments import (
    AcquisitionRequest,
    DeviceError,
    DeviceIdentity,
    DeviceStatus,
    Measurement,
    Unsupported,
    ValidationError,
    readable_serial,
)
from lab_control.instruments.base import Profile, ScpiAdapter, identity_from_scpi, number, read_command
from lab_control.transport.streams import SerialTransport

CHANNELS = tuple(range(1, 9))
READINGS = {
    "voltage": ("MEAS:VOLT?", "V"),
    "current": ("MEAS:CURR?", "A"),
}
NUMERIC = re.compile(rb"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[Ee][+-]?[0-9]+)?")


def dh8318() -> Profile:
    return Profile(
        "duoheyi.dh8318",
        ("DUOHEYI",),
        ("DH-8318",),
        "electronic_load",
        CHANNELS,
        "《单通道、多通道 可编程直流电子负载 用户使用手册》",
        "V1.1，第 19、22–24、27–28 页",
        {f"measured_{q}": read_command(f"measured_{q}", query) for q, (query, _) in READINGS.items()},
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
        self.selected_channels = self._selection()

    def capabilities(self):
        return replace(super().capabilities(), channels=self.selected_channels)

    def read_status(self) -> DeviceStatus:
        self.refresh_capabilities()
        return DeviceStatus({"selected_channels": self.selected_channels})

    def _read_values(self, channels: tuple[int, ...], quantities: tuple[str, ...]):
        selected = self._selection()
        self.selected_channels = selected
        if not set(channels).issubset(selected):
            raise DeviceError(f"DH-8318 当前可读通道为 {selected}，请重新选择通道", "configuration_changed")
        messages = {q: line(self.transport.query(READINGS[q][0])) for q in quantities}
        after = self._selection()
        if selected != after:
            self.selected_channels = after
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
        q = operation.removeprefix("measured_")
        return self._read_values((channel,), (q,))[q][channel]

    def acquire_measurements(self, request: AcquisitionRequest):
        if request.kind != "scalar" or request.initiate or request.quantity not in ("", *READINGS):
            raise Unsupported("DH-8318 支持只读采集电压和电流")
        quantities = (request.quantity,) if request.quantity else tuple(READINGS)
        values = self._read_values(request.channels, quantities)
        return tuple(
            Measurement(q, READINGS[q][1], [values[q][ch]], ch) for ch in request.channels for q in quantities
        )
