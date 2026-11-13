"""Four operator-specified commands. Received serial bytes carry no verified state."""

from typing import Protocol

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
    MeasurementBatch,
    ParameterReading,
    Unsupported,
    ValidationError,
)

ADAPTER_ID = "custom.dc_switch"
MODEL = "DC / CH3 / CH4 开关板"
SOURCE = "自制板串口协议（现场提供）"
COMMANDS = {"dc_on": b"DC-on", "dc_off": b"DC-off", "ch3_on": b"CH3-on", "ch4_on": b"CH4-on"}
SERIAL_STATUS = "switch_board_serial_status"


class SwitchWire(Protocol):
    def send(self, payload: bytes) -> None: ...
    def receive(self, *, wait: bool = False) -> bytes: ...
    def close(self) -> None: ...


def capability_schema(_config: ConnectionConfig) -> CapabilitySet:
    operations = tuple(
        Capability(
            name, writable=True, requires_confirmation=True, source=SOURCE, section=wire.decode("ascii")
        )
        for name, wire in COMMANDS.items()
    )
    return CapabilitySet(
        "output_switch",
        (1,),
        (
            *operations,
            Capability(
                SERIAL_STATUS, readable=True, source=SOURCE, section="本机会话，仅被动读取串口接收缓冲区"
            ),
        ),
    )


class SwitchBoardAdapter:
    adapter_id = ADAPTER_ID

    def __init__(self, config: ConnectionConfig, transport: SwitchWire) -> None:
        self.transport = transport
        self.schema = capability_schema(config)
        self.identity = DeviceIdentity("自制", MODEL, confidence="operator_selected")
        self.last_command = ""
        self.last_rx = b""

    def identify(self) -> DeviceIdentity:
        return self.identity

    def capabilities(self) -> CapabilitySet:
        return self.schema

    def _status(self) -> dict[str, str]:
        printable = "".join(chr(b) if 32 <= b <= 126 else "." for b in self.last_rx)
        return {
            "最近完整发送": self.last_command or "无",
            "板端输出状态": "未知",
            "最近串口接收 HEX": self.last_rx.hex(" ").upper() or "无",
            "可打印 ASCII": printable or "无",
        }

    def read_status(self) -> DeviceStatus:
        received = self.transport.receive()
        if received:
            self.last_rx = received
        return DeviceStatus(self._status())

    def read_settings(self, operations: tuple[str, ...], channel: int = 1) -> tuple[ParameterReading, ...]:
        if channel != 1 or any(operation != SERIAL_STATUS for operation in operations):
            raise Unsupported("该板没有输出状态查询命令")
        if not operations:
            return ()
        return (ParameterReading(SERIAL_STATUS, 1, self.read_status().values, "session_cache"),)

    def apply(self, command: DeviceCommand) -> ApplyResult:
        if command.operation not in COMMANDS:
            raise Unsupported("未知的开关板命令")
        if type(command.channel) is not int or command.channel != 1:
            raise ValidationError("通过独立的 CH3-on 或 CH4-on 动作选择通道")
        self.schema.get(command.operation).validate(command.parameters)
        if not command.confirmed:
            raise DeviceError("发送开关板命令需要确认", "confirmation_required")
        payload = COMMANDS[command.operation]
        self.last_rx = b""
        try:
            self.transport.send(payload)
            self.last_command = payload.decode("ascii")
            self.last_rx = self.transport.receive(wait=True)
        except DeviceError as exc:
            return ApplyResult("uncertain", command.operation, {}, self._status(), error=str(exc))
        return ApplyResult("success", command.operation, {}, self._status(), completed_steps=1)

    def acquire(self, _request: AcquisitionRequest) -> MeasurementBatch:
        raise Unsupported("开关板协议没有测量命令")

    def close(self) -> None:
        self.transport.close()
