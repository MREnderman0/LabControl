import socket
import struct
import threading
from dataclasses import replace

import pytest

from lab_control.application.config import ConnectionConfig
from lab_control.application.static_addresses import StaticAddress
from lab_control.domain.instruments import AcquisitionRequest, DeviceCommand, DeviceError, Unsupported
from lab_control.instruments.registry import DEVICE_DEFINITIONS, create_adapter, normalize_discovery_config

from ..adapter import ADAPTER_ID, RecorderAdapter
from ..protocol import CHANNELS, ORDERS, RecorderTransport, Settings

CONFIG = ConnectionConfig("tcp://localhost:502", ADAPTER_ID, "tcp")


class Banks:
    def __init__(self, ints=range(16), floats=b"\x00" * 64):
        self.blocks = {0: struct.pack(">16h", *ints), 100: floats}
        self.requests = []

    def inputs(self, station, offset, count):
        self.requests.append((station, offset, count))
        return self.blocks[offset]


def test_static_dialog_and_scan_metadata():
    entry = StaticAddress("test", "192.0.2.15", 502, ADAPTER_ID)
    cfg = normalize_discovery_config(entry.config(), ())
    assert cfg.transport == "tcp" and cfg.resource == entry.resource
    definition = DEVICE_DEFINITIONS[ADAPTER_ID]
    assert definition.socket_port == 502 and definition.monitoring_only
    tcp = definition.transport_factory(cfg, None)
    assert (tcp.host, tcp.port) == ("192.0.2.15", 502)


def test_raw_batch_preserves_sign_order_and_refuses_empty_input():
    banks = Banks([0, -32640, -12, *range(13)])
    dev = RecorderAdapter(CONFIG, banks)
    assert dev.identify().key is None
    assert dev.capability_details["acquisition_channels"] == [ch for ch in CHANNELS if ch != 2]
    batch = dev.acquire(AcquisitionRequest(channels=(3, 1)))
    assert [(m.channel, m.unit, m.values[0]) for m in batch.measurements] == [
        (3, "count", -12),
        (1, "count", 0),
    ]
    with pytest.raises(DeviceError, match="CH2"):
        dev.acquire(AcquisitionRequest(channels=(1, 2)))
    assert dev.read_status().values["unconfirmed_channels"] == [2]
    with pytest.raises(Unsupported):
        dev.apply(DeviceCommand("input_raw"))
    with pytest.raises(DeviceError):
        dev.acquire(AcquisitionRequest(channels=(17,)))
    assert set(banks.requests) == {(1, 0, 16), (1, 100, 32)}


@pytest.mark.parametrize("order", list(ORDERS))
def test_configured_float_byte_order_and_units(order):
    fmt = ORDERS[order]
    encoded = b"".join(bytes(struct.pack(">f", v)[i] for i in fmt) for v in [12.5, -0.75, *range(14)])
    dev = RecorderAdapter(
        replace(CONFIG, options=("station=17", "channels=2,1", "unit=°C", f"float_order={order}")),
        Banks(floats=encoded),
    )
    dev.identify()
    ms = dev.acquire(AcquisitionRequest(channels=(2, 1))).measurements
    assert [(m.channel, m.values, m.quantity, m.unit) for m in ms] == [
        (2, (-0.75,), "temperature", "°C"),
        (1, (12.5,), "temperature", "°C"),
    ]
    with pytest.raises(DeviceError):
        dev.acquire(AcquisitionRequest(channels=(3,)))


@pytest.mark.parametrize("option", ["unit=V", "channels=1,1", "station=0", "channels=17", "float_order=bad"])
def test_reject_misconfiguration(option):
    with pytest.raises(DeviceError):
        Settings.from_config(replace(CONFIG, options=(option,)))


@pytest.mark.parametrize("failure", ["", "exception", "transaction", "length", "truncated", "timeout"])
def test_factory_binary_wire_and_no_replay(failure):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    observed = []

    def serve():
        with listener, listener.accept()[0] as s:
            for i in (1, 2):
                req = b""
                while len(req) < 12:
                    chunk = s.recv(12 - len(req))
                    if not chunk:
                        return
                    req += chunk
                observed.append(req)
                tid, proto, length, unit, fc, start, n = struct.unpack(">HHHBBHH", req)
                if failure == "timeout":
                    s.recv(1)
                    return
                head = struct.pack(
                    ">HHHB",
                    tid + (failure == "transaction"),
                    0,
                    3 if failure in ("exception", "length") else 3 + 2 * n,
                    unit,
                )
                body = bytes([0x84, 2]) if failure == "exception" else bytes([4, 2 * n]) + b"\x12\x34" * n
                s.sendall(head[:3])
                s.sendall(head[3:])
                s.sendall(body[:3] if failure == "truncated" else body)
                if failure:
                    return

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    dev = create_adapter(
        replace(CONFIG, resource="tcp://127.0.0.1:" + str(listener.getsockname()[1]), timeout=0.3), None
    )
    try:
        if failure:
            with pytest.raises(DeviceError):
                dev.identify()
            assert isinstance(dev.transport, RecorderTransport) and dev.transport.sock is None
        else:
            assert not dev.identify().serial
    finally:
        dev.close()
        t.join(2)
    assert not t.is_alive()
    assert observed[0] == bytes.fromhex("000100000006010400000010")
    assert len(observed) == (1 if failure else 2)
