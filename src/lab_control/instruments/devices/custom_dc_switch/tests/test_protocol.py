import json
from collections import deque
from pathlib import Path

import pytest
from pyvisa.constants import ControlFlow, Parity, SerialTermination, StopBits

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


class Handle:
    def __init__(self):
        self.writes = []
        self.rx = deque()
        self.reply = b""
        self.closed = False

    @property
    def bytes_in_buffer(self):
        return len(self.rx)

    def write_raw(self, payload):
        self.writes.append(payload)
        self.rx.extend(self.reply)
        return len(payload)

    def read_bytes(self, count, *, break_on_termchar):
        assert not break_on_termchar
        return bytes(self.rx.popleft() for _ in range(count))

    def close(self):
        self.closed = True


class Manager:
    def __init__(self):
        self.handles = []
        self.next_ack = b""

    def open_resource(self, resource, **_kwargs):
        assert resource == CONFIG.resource
        handle = Handle()
        handle.reply = self.next_ack
        self.handles.append(handle)
        return handle

    def close(self):
        assert all(handle.closed for handle in self.handles)


def test_zero_tx_identity_status_unknown_and_no_acquisition():
    manager = Manager()
    visa = VisaRuntime(manager=manager)
    wire = create_transport(CONFIG, visa)
    wire.open()
    board = SwitchBoardAdapter(CONFIG, wire)
    try:
        assert board.identify().key is None and board.identify().confidence == "operator_selected"
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
    manager = Manager()
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
        assert (h.baud_rate, h.data_bits, h.parity, h.stop_bits) == (115200, 8, Parity.none, StopBits.one)
        assert (h.flow_control, h.end_input, h.end_output) == (
            ControlFlow.none,
            SerialTermination.none,
            SerialTermination.none,
        )
        assert h.write_termination is None and h.read_termination is None
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
    manager = Manager()
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

        def receive(self, *, wait=False):
            assert not wait
            return b""

    wire = Wire()
    board = SwitchBoardAdapter(CONFIG, wire)
    result = board.apply(DeviceCommand("dc_on", confirmed=True))
    assert result.outcome == "uncertain" and result.readback["最近完整发送"] == "无" and wire.writes == 1


def test_only_complete_current_reply_confirms_even_when_split_across_status_reads():
    class Wire:
        incoming = b""
        reply = b""

        def receive(self, *, wait=False):
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
    manager = Manager()
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
