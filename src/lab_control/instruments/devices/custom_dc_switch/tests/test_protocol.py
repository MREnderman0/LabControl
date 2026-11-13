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
        self.closed = False

    @property
    def bytes_in_buffer(self):
        return len(self.rx)

    def write_raw(self, payload):
        self.writes.append(payload)
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
        handle.rx.extend(self.next_ack)
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
        assert board.read_status().values["板端输出状态"] == "未知"
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
            if manager.handles:
                manager.handles[0].rx.extend(bytes.fromhex(FIXTURE["sample_rx_hex"]))
            else:
                manager.next_ack = bytes.fromhex(FIXTURE["sample_rx_hex"])
            result = board.apply(DeviceCommand(op, confirmed=True))
            assert result.outcome == "success" and result.readback["板端输出状态"] == "未知"
            assert result.readback["最近完整发送"] == bytes.fromhex(expected).decode("ascii")
            assert result.readback["最近串口接收 HEX"] == FIXTURE["sample_rx_hex"].upper()
        assert len(manager.handles) == 1
        h = manager.handles[0]
        assert (h.baud_rate, h.data_bits, h.parity, h.stop_bits) == (115200, 8, Parity.none, StopBits.one)
        assert (h.flow_control, h.end_input, h.end_output) == (
            ControlFlow.none,
            SerialTermination.none,
            SerialTermination.none,
        )
        assert h.write_termination is None and h.read_termination is None
        last = board.apply(DeviceCommand("ch3_on", confirmed=True))
        assert last.readback["最近完整发送"] == "CH3-on" and last.readback["最近串口接收 HEX"] == "无"
        h.rx.extend(b"later\x00")
        assert board.read_settings((SERIAL_STATUS,))[0].value["最近串口接收 HEX"] == "6C 61 74 65 72 00"
        assert h.writes == [b"DC-on", b"DC-off", b"CH3-on", b"CH4-on", b"CH3-on"]
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
            raise AssertionError("receive after failed write")

    wire = Wire()
    board = SwitchBoardAdapter(CONFIG, wire)
    result = board.apply(DeviceCommand("dc_on", confirmed=True))
    assert result.outcome == "uncertain" and result.readback["最近完整发送"] == "无" and wire.writes == 1
