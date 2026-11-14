import json
from collections import deque
from pathlib import Path

import pytest
from serial.serialutil import SerialBase

from lab_control.application.controls import data_sources
from lab_control.discovery.providers import SerialDiscoveryProvider
from lab_control.discovery.types import DiscoveryCandidate
from lab_control.domain.connections import ConnectionConfig
from lab_control.domain.instruments import AcquisitionRequest, DeviceCommand, DeviceError, Unsupported
from lab_control.instruments.devices.custom_dc_switch.adapter import (
    ADAPTER_ID,
    SERIAL_STATUS,
    SwitchBoardAdapter,
)
from lab_control.instruments.devices.custom_dc_switch.transport import create_transport
from lab_control.transport.visa import VisaRuntime

FIXTURE = json.loads((Path(__file__).parent / "fixtures/control.json").read_text(encoding="utf-8"))
CONFIG = ConnectionConfig("ASRL99::INSTR", ADAPTER_ID)


class Handle(SerialBase):
    """Exercise pySerial's real line-state setters with no operating-system handle."""

    def __init__(self, **settings):
        self.events = []
        self.writes = []
        self.rx = deque()
        self.reply = b""
        self.idn = b"ESP32-S32 Board!\r\n"
        super().__init__(**settings)

    def _update_dtr_state(self):
        self.events.append(("live_dtr", self.dtr))

    def _update_rts_state(self):
        self.events.append(("live_rts", self.rts))

    def open(self):
        self.events.append(("open", self.port, self.dtr, self.rts))
        self.is_open = True

    @property
    def in_waiting(self):
        return len(self.rx)

    def write(self, payload):
        assert self.is_open
        self.writes.append(payload)
        self.rx.extend(self.idn if payload == b"*IDN?\n" else self.reply)
        return len(payload)

    def read(self, count):
        assert self.is_open
        return bytes(self.rx.popleft() for _ in range(count))

    def close(self):
        if self.is_open:
            self.events.append(("close", self.port, self.dtr, self.rts))
            self.is_open = False


class Manager:
    def __init__(self, monkeypatch):
        self.handles = []
        self.next_ack = b""
        self.next_idn = b"ESP32-S32 Board!\r\n"
        monkeypatch.setattr("serial.Serial", self.serial)

    def serial(self, **settings):
        assert settings["port"] is None  # Configure control lines before native open.
        handle = Handle(**settings)
        handle.reply = self.next_ack
        handle.idn = self.next_idn
        self.handles.append(handle)
        return handle

    def open_resource(self, *_args, **_kwargs):
        pytest.fail("The switch board must never open a VISA serial handle")

    def close(self):
        assert all(not handle.is_open for handle in self.handles)


def test_status_and_capabilities_do_not_write_before_connection(monkeypatch):
    manager = Manager(monkeypatch)
    visa = VisaRuntime(manager=manager)
    wire = create_transport(CONFIG, visa)
    wire.open()
    board = SwitchBoardAdapter(CONFIG, wire)
    try:
        assert board.read_status().values == {"操作结果": "尚未发送"}
        assert board.read_settings(()) == () and not data_sources(CONFIG)
        with pytest.raises(Unsupported):
            board.acquire(AcquisitionRequest())
        with pytest.raises(DeviceError):
            board.apply(DeviceCommand("ch4_on"))
        assert manager.handles == []
        board.close()
        assert manager.handles == []
    finally:
        board.close()
        visa.close()


def test_exact_commands_passive_receipts_and_mutual_exclusion_without_companion_writes(monkeypatch):
    port = DiscoveryCandidate(
        ConnectionConfig("COM99", transport="serial"), "serial", {"vid": 0x10C4, "pid": 0xEA60}
    )
    monkeypatch.setattr(SerialDiscoveryProvider, "enumerate", lambda _: (port,))
    manager = Manager(monkeypatch)
    visa = VisaRuntime(manager=manager)
    wire = create_transport(CONFIG, visa)
    wire.open()
    board = SwitchBoardAdapter(CONFIG, wire)
    try:
        for op, expected in FIXTURE["requests"].items():
            ack = (FIXTURE["replies"][op] + "\r\n").encode()
            if manager.handles:
                manager.handles[0].reply = ack
            else:
                manager.next_ack = ack
            result = board.apply(DeviceCommand(op, confirmed=True))
            assert result.outcome == "success" and result.acknowledged
            assert result.readback["最近完整发送"] == op
            assert (
                result.readback["板端输出状态"]
                == result.readback["最近串口接收 UTF-8"]
                == FIXTURE["replies"][op]
            )
            assert result.readback["最近串口接收 HEX"] == ack.hex(" ").upper()
            assert manager.handles[0].writes[-1] == bytes.fromhex(expected)
        assert len(manager.handles) == 1
        h = manager.handles[0]
        assert (h.baudrate, h.bytesize, h.parity, h.stopbits) == (115200, 8, "N", 1)
        assert (h.xonxoff, h.rtscts, h.dsrdtr) == (False, False, False)
        assert h.events == [("open", "COM99", False, False)]
        h.reply = b""
        last = board.apply(DeviceCommand("ch3_on", confirmed=True))
        assert last.readback["最近完整发送"] == "ch3_on" and last.readback["最近串口接收 HEX"] == "无"
        assert last.acknowledged is False
        h.rx.extend(b"later\x00")
        assert board.read_settings((SERIAL_STATUS,))[0].value == "已发送"
        assert h.writes == [b"dc_on\n", b"dc_off\n", b"ch3_on\n", b"ch4_on\n", b"ch3_on\n"]
        with pytest.raises(Unsupported):
            board.apply(DeviceCommand("ch4_off", confirmed=True))
    finally:
        board.close()
        visa.close()


def test_port_reused_between_binding_and_output_never_opens_handle(monkeypatch):
    manager = Manager(monkeypatch)
    visa = VisaRuntime(manager=manager)
    config = ConnectionConfig(CONFIG.resource, ADAPTER_ID, usb_instance_id="original")
    wire = create_transport(config, visa)
    wire.open()
    board = SwitchBoardAdapter(config, wire)
    replacement = DiscoveryCandidate(
        ConnectionConfig("COM99", transport="serial", usb_instance_id="different"),
        "serial",
        {"vid": 0x10C4, "pid": 0xEA60},
    )
    monkeypatch.setattr(SerialDiscoveryProvider, "enumerate", lambda _: (replacement,))
    result = board.apply(DeviceCommand("dc_on", confirmed=True))
    assert result.outcome == "uncertain" and manager.handles == []
    board.close()
    visa.close()


def test_short_write_is_uncertain_without_retry():
    class Wire:
        writes = 0

        def send(self, _):
            self.writes += 1
            raise DeviceError("short write", "disconnected")

        def receive(self, *, timeout=0.0):
            assert not timeout
            return b""

    wire = Wire()
    board = SwitchBoardAdapter(CONFIG, wire)
    result = board.apply(DeviceCommand("dc_on", confirmed=True))
    assert result.outcome == "uncertain" and result.readback["最近完整发送"] == "无" and wire.writes == 1


def test_only_complete_current_reply_confirms_even_when_split_across_status_reads():
    class Wire:
        incoming = b""
        reply = b""

        def receive(self, *, timeout=0.0):
            value, self.incoming = self.incoming, b""
            return value

        def send(self, payload):
            self.incoming = self.reply

    wire = Wire()
    board = SwitchBoardAdapter(CONFIG, wire)
    command = DeviceCommand("dc_on", confirmed=True)
    ack = "DC已打开".encode()
    wire.incoming = ack + b"\r\n"
    wire.reply = "请输入命令".encode() + b"\r\nRF" + b"3" + "已打开".encode() + b"\r\n" + ack[:3]
    result = board.apply(command)
    assert not result.acknowledged and "请输入命令" in result.readback["最近串口接收 UTF-8"]
    wire.incoming = ack[3:] + b"\r"
    assert board.read_status().values["操作结果"] == "已发送"
    wire.incoming = b"\n"
    assert board.read_status().values["操作结果"] == "DC已打开"
    wire.reply = b""
    assert not board.apply(command).acknowledged
    wire.reply = ack + b"\r\n"
    assert not board.apply(command).acknowledged  # The preceding identical request is still outstanding.
    wire.incoming = ack + b"\r\n"
    assert board.read_status().values["操作结果"] == "DC已打开"


def test_first_click_waits_for_firmware_then_writes_once(monkeypatch):
    from lab_control.instruments.devices.custom_dc_switch import transport as serial

    port = DiscoveryCandidate(
        ConnectionConfig("COM99", transport="serial"), "serial", {"vid": 0x10C4, "pid": 0xEA60}
    )
    monkeypatch.setattr(SerialDiscoveryProvider, "enumerate", lambda _: (port,))
    manager = Manager(monkeypatch)
    events = []

    def startup(seconds):
        assert seconds == serial.STARTUP_SETTLE_SECONDS
        (handle,) = manager.handles
        assert handle.writes == []
        handle.rx.extend(b"rst:0x1 (POWERON_RESET)\r\n")
        handle.reply = "DC已关闭\r\n".encode()
        events.append("boot")

    monkeypatch.setattr(serial.time, "sleep", startup)
    visa = VisaRuntime(manager=manager)
    wire = create_transport(CONFIG, visa)
    wire.open()
    board = SwitchBoardAdapter(CONFIG, wire)
    try:
        assert not manager.handles and not events
        for _ in range(2):
            result = board.apply(DeviceCommand("dc_off", confirmed=True))
            assert result.user_message == "DC已关闭"
            assert result.readback["最近串口接收 UTF-8"] == "DC已关闭"
        assert manager.handles[0].writes == [b"dc_off\n", b"dc_off\n"] and events == ["boot"]
    finally:
        board.close()
        visa.close()


@pytest.mark.parametrize("operation", FIXTURE["requests"])
def test_confirmation_after_serial_padding_and_unrelated_line(operation):
    ack = FIXTURE["replies"][operation].encode()
    chunks = deque([b"boot log\r\n", b"\xff" * 64 + ack[:3], ack[3:] + b"\r", b"\n"])
    writes = []

    class Wire:
        def receive(self, *, timeout=0.0):
            if not timeout:
                return b"\xff" * 64
            return chunks.popleft() if chunks else b""

        def send(self, payload):
            writes.append(payload)

    result = SwitchBoardAdapter(CONFIG, Wire()).apply(DeviceCommand(operation, confirmed=True))
    assert result.acknowledged and result.user_message == FIXTURE["replies"][operation]
    assert result.readback["最近串口接收 HEX"].startswith("62 6F 6F 74 20 6C 6F 67 0D 0A FF FF")
    assert writes == [(operation + "\n").encode()] and not chunks


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"\xff" * 64,
        b"DC" + b"\xff" + "已打开\r\n".encode(),
        "错误 DC已打开\r\n".encode(),
        "DC已打开".encode(),
        "DC已关闭\r\n".encode(),
    ],
)
def test_non_matching_bytes_never_confirm_dc_on(raw):
    class Wire:
        data = raw

        def receive(self, *, timeout=0.0):
            if not timeout:
                return b""
            chunk, self.data = self.data, b""
            return chunk

        def send(self, payload):
            assert payload == b"dc_on\n"

    result = SwitchBoardAdapter(CONFIG, Wire()).apply(DeviceCommand("dc_on", confirmed=True))
    assert result.acknowledged is False and result.user_message == "已发送"


def test_ack_for_later_command_resolves_earlier_missing_reply():
    class Wire:
        response = b""

        def receive(self, *, timeout=0.0):
            data, self.response = self.response, b""
            return data

        def send(self, payload):
            assert payload == (operation + "\n").encode()
            self.response = response

    board = SwitchBoardAdapter(CONFIG, Wire())
    for operation, response, expected in [
        ("dc_off", b"", "已发送"),
        ("dc_on", b"\xff" * 64 + "DC已打开\r\n".encode(), "DC已打开"),
        ("dc_off", "DC已关闭\r\n".encode(), "DC已关闭"),
    ]:
        assert board.apply(DeviceCommand(operation, confirmed=True)).user_message == expected


@pytest.mark.parametrize(
    "chunks,valid",
    [
        ([b"rst:0x1\r\n", b"\xff" * 64 + b"ESP32-S", b"32 Board!\r", b"\n"], True),
        ([b"ESP32-S32 Board!"], False),
        ([b"prefix ESP32-S32 Board!\r\n"], False),
        ([b"ESP32-S32 Board! v2\r\n"], False),
        ([b"wrong\r\n"], False),
        ([], False),
    ],
)
def test_identity_checks_complete_firmware_line(chunks, valid):
    class Wire:
        def receive(self, *, timeout=0.0):
            if not timeout:
                return b"ESP32-S32 Board!\r\n"
            return chunks.pop(0) if chunks else b""

        def send(self, payload):
            writes.append(payload)

    writes = []
    board = SwitchBoardAdapter(CONFIG, Wire())
    if valid:
        result = board.identify()
        assert result.key is None and result.raw == "ESP32-S32 Board!" and result.confidence == "model_only"
    else:
        with pytest.raises(DeviceError, match="身份校验"):
            board.identify()
    assert writes == [b"*IDN?\n"]
