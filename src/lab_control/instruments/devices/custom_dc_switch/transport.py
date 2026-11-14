"""LF-framed serial writes and bounded reads of UTF-8 reply lines."""

import re
import time

from lab_control.discovery.providers import SerialDiscoveryProvider
from lab_control.domain.connections import ConnectionConfig
from lab_control.domain.instruments import DeviceError, ValidationError
from lab_control.transport.diagnostics import measured, synchronized
from lab_control.transport.streams import SerialTransport
from lab_control.transport.visa import VisaRuntime

# A freshly powered board has no ready banner.
STARTUP_SETTLE_SECONDS = 1.5


class SwitchTransport(SerialTransport):
    """Open the selected bridge once for the identity handshake and subsequent commands."""

    def __init__(self, config: ConnectionConfig, port: str) -> None:
        # The CP210x modem lines are wired to ESP32 reset/boot. SerialTransport
        # preselects inactive DTR and RTS on a closed handle before opening it.
        super().__init__(
            port,
            115200,
            config.timeout,
            bytesize=8,
            parity="N",
            stopbits=1,
            xonxoff=False,
            rtscts=False,
            dsrdtr=False,
        )
        self.usb_instance_id = config.usb_instance_id
        self.bound = False

    @synchronized
    def open(self) -> None:
        self.bound = True

    @synchronized
    def close(self) -> None:
        self.bound = False
        super().close()

    def _open_serial(self) -> None:
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
        try:
            super().open()
            time.sleep(STARTUP_SETTLE_SECONDS)
            # Boot text and stale lines predate the first output request.
            # Clear only the bytes already buffered, before writing the request.
            self.receive()
        except Exception:
            self.close()
            raise

    @synchronized
    def send(self, payload: bytes) -> None:
        self._open_serial()
        self.write(payload)

    @synchronized
    @measured("read")
    def receive(self, *, timeout: float = 0.0) -> bytes:
        # Serial.println terminates each reply with LF. Read across USB packet boundaries.
        if self.handle is None:
            return b""
        end = time.monotonic() + timeout
        data = bytearray()
        try:
            while True:
                available = min(4096 - len(data), self.handle.in_waiting)
                if available:
                    block = self.handle.read(available)
                    if len(block) != available:
                        raise DeviceError("串口接收缓冲读取不完整", "disconnected")
                    data.extend(block)
                if data.endswith(b"\n") or len(data) == 4096 or time.monotonic() >= end:
                    return bytes(data)
                time.sleep(0.005)
        except DeviceError:
            raise
        except OSError as exc:
            raise DeviceError(str(exc), "disconnected") from exc


def serial_port(config: ConnectionConfig) -> str:
    match = re.fullmatch(r"(?:COM([1-9][0-9]*)|ASRL([1-9][0-9]*)::INSTR)", config.resource, re.I)
    if config.transport not in {"serial", "visa"} or not match:
        raise ValidationError("请选择开关板的 Windows COM 口")
    return "COM" + (match[1] or match[2])


def create_transport(config: ConnectionConfig, runtime: VisaRuntime) -> SwitchTransport:
    return SwitchTransport(config, serial_port(config))
