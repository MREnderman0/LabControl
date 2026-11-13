"""LF-framed serial writes and bounded reads of UTF-8 reply lines."""

import re
import time

from lab_control.discovery.providers import SerialDiscoveryProvider
from lab_control.domain.connections import ConnectionConfig
from lab_control.domain.instruments import DeviceError, ValidationError
from lab_control.transport.diagnostics import measured, synchronized
from lab_control.transport.visa import RawSerialVisaTransport, VisaRuntime


class SwitchTransport(RawSerialVisaTransport):
    """Bind the application session without asking Windows to open the COM handle."""

    def __init__(self, config: ConnectionConfig, runtime: VisaRuntime) -> None:
        super().__init__(runtime, config.resource, config.timeout, terminator="", baud_rate=115200)
        self.port = "COM" + config.resource[4:].split("::", 1)[0]
        self.usb_instance_id = config.usb_instance_id
        self.bound = False

    def open(self) -> None:
        self.bound = True

    def close(self) -> None:
        self.bound = False
        super().close()

    def _open_for_output(self) -> None:
        if not self.bound:
            raise DeviceError("请重新绑定开关板串口", "manual_reconnect_required")
        if self.handle is not None:
            return
        from . import DEFINITION

        if not any(
            port.config.resource.upper() == self.port
            and DEFINITION.matches_manual_port(port.descriptor)
            and (not self.usb_instance_id or port.config.usb_instance_id == self.usb_instance_id)
            for port in SerialDiscoveryProvider().enumerate()
        ):
            raise DeviceError("已绑定的 USB 转接器不在所选串口，请重新核对接线", "manual_reconnect_required")
        # Opening the handle itself may pulse DTR/RTS. Only an explicitly
        # confirmed output operation may cross this boundary.
        try:
            super().open()
        except Exception:
            self.close()
            raise

    @synchronized
    @measured("write")
    def send(self, payload: bytes) -> None:
        try:
            self._open_for_output()
            size = self.handle.write_raw(payload)
            if size != len(payload):
                raise DeviceError(f"串口写入不完整（{size}/{len(payload)} 字节）", "disconnected")
        except Exception as exc:
            raise self._error(exc) from exc

    @synchronized
    @measured("read")
    def receive(self, *, wait: bool = False) -> bytes:
        # Serial.println terminates each reply with LF. Read across USB packet boundaries.
        if self.handle is None:
            return b""
        end = time.monotonic() + (0.25 if wait else 0)
        data = bytearray()
        try:
            while True:
                available = min(4096 - len(data), self.handle.bytes_in_buffer)
                if available:
                    block = self.handle.read_bytes(available, break_on_termchar=False)
                    if len(block) != available:
                        raise DeviceError("串口接收缓冲读取不完整", "disconnected")
                    data.extend(block)
                if data.endswith(b"\n") or len(data) == 4096 or time.monotonic() >= end:
                    return bytes(data)
                time.sleep(0.005)
        except Exception as exc:
            raise self._error(exc) from exc


def create_transport(config: ConnectionConfig, runtime: VisaRuntime) -> SwitchTransport:
    if config.transport != "visa" or not re.fullmatch(r"ASRL[1-9][0-9]*::INSTR", config.resource):
        raise ValidationError("请选择开关板的 Windows COM 口")
    return SwitchTransport(config, runtime)
