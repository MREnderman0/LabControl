"""Line-oriented commands and UTF-8 acknowledgements from the switch-board firmware."""

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
ACKS = {"dc_on": "DC已打开", "dc_off": "DC已关闭", "ch3_on": "RF3已打开", "ch4_on": "RF4已打开"}
MAX_RX = 4096
SERIAL_STATUS = "switch_board_serial_status"


class SwitchWire(Protocol):
    def send(self, payload: bytes) -> None: ...
    def receive(self, *, wait: bool = False) -> bytes: ...
    def close(self) -> None: ...


def capability_schema(_config: ConnectionConfig) -> CapabilitySet:
    operations = tuple(
        Capability(name, writable=True, requires_confirmation=True, source=SOURCE, section=name)
        for name in ACKS
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
        self.partial_line = b""
        self.drop_line = False
        self.pending: dict[str, int] = {}
        self.confirmed = False

    def identify(self) -> DeviceIdentity:
        return self.identity

    def capabilities(self) -> CapabilitySet:
        return self.schema

    def _receive(self, raw: bytes) -> None:
        if not raw:
            return
        self.last_rx = (self.last_rx + raw)[-MAX_RX:]
        chunks = raw.split(b"\n")
        for chunk in chunks[:-1]:
            line = self.partial_line + chunk
            if not self.drop_line and len(line) <= MAX_RX:
                reply = line.removesuffix(b"\r").decode("utf-8", errors="replace")
                if reply.startswith("请输入命令"):
                    self.confirmed = False
                remaining = self.pending.get(reply, 0)
                if remaining:
                    self.pending[reply] = remaining - 1
                    if remaining == 1 and reply == ACKS.get(self.last_command):
                        self.confirmed = True
            self.partial_line = b""
            self.drop_line = False
        if not self.drop_line:
            self.partial_line += chunks[-1]
            if len(self.partial_line) > MAX_RX:
                self.drop_line = True
                self.partial_line = b""

    def _status(self) -> dict[str, str]:
        return {
            "最近完整发送": self.last_command or "无",
            "板端输出状态": ACKS[self.last_command] if self.confirmed else "未知",
            "最近串口接收 UTF-8": self.last_rx.decode("utf-8", errors="replace").rstrip("\r\n") or "无",
            "最近串口接收 HEX": self.last_rx.hex(" ").upper() or "无",
        }

    def read_status(self) -> DeviceStatus:
        self._receive(self.transport.receive())
        return DeviceStatus(self._status())

    def read_settings(self, operations: tuple[str, ...], channel: int = 1) -> tuple[ParameterReading, ...]:
        if channel != 1 or any(operation != SERIAL_STATUS for operation in operations):
            raise Unsupported("该板没有输出状态查询命令")
        if not operations:
            return ()
        return (ParameterReading(SERIAL_STATUS, 1, self.read_status().values, "session_cache"),)

    def apply(self, command: DeviceCommand) -> ApplyResult:
        if command.operation not in ACKS:
            raise Unsupported("未知的开关板命令")
        if type(command.channel) is not int or command.channel != 1:
            raise ValidationError("通过 ch3_on 或 ch4_on 选择射频通路")
        self.schema.get(command.operation).validate(command.parameters)
        if not command.confirmed:
            raise DeviceError("需要显式请求开关板输出", "confirmation_required")
        # Consume replies already buffered for preceding requests before sending.
        # A partial pre-existing line cannot acknowledge the next command.
        try:
            buffered = self.transport.receive()
            self._receive(buffered)
            if len(buffered) == MAX_RX:
                raise DeviceError("串口仍有积压数据，请先读取状态", "receive_backlog")
            if self.partial_line:
                self.drop_line = True
                self.partial_line = b""
            self.confirmed = False
            self.transport.send(command.operation.encode("ascii") + b"\n")
        except DeviceError as exc:
            self.confirmed = False
            return ApplyResult("uncertain", command.operation, {}, self._status(), error=str(exc))
        self.last_command = command.operation
        self.last_rx = b""
        reply = ACKS[command.operation]
        self.pending[reply] = self.pending.get(reply, 0) + 1
        error = ""
        try:
            self._receive(self.transport.receive(wait=True))
        except DeviceError as exc:
            error = str(exc)
        return ApplyResult(
            "success",
            command.operation,
            {},
            self._status(),
            completed_steps=1,
            error=error,
            acknowledged=self.confirmed,
        )

    def acquire(self, _request: AcquisitionRequest) -> MeasurementBatch:
        raise Unsupported("开关板协议没有测量命令")

    def close(self) -> None:
        self.transport.close()
