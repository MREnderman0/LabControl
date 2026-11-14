from types import SimpleNamespace

import pytest

from lab_control.application.runtime import ApplicationRuntime
from lab_control.discovery.providers import (
    ConfiguredDiscoveryProvider,
    SerialDiscoveryProvider,
    VisaDiscoveryProvider,
)
from lab_control.discovery.service import DiscoveryService
from lab_control.domain.connections import ConnectionConfig
from lab_control.domain.instruments import DeviceError, DeviceIdentity
from lab_control.instruments.devices.duoheyi_dh8318 import DEFINITION
from lab_control.transport.base import MockTransport


def test_configured_serial_without_bridge_id_needs_fresh_matching_instrument_identity(tmp_path, monkeypatch):
    config = ConnectionConfig("COM7", "duoheyi.dh8318", "serial", expected_serial="TEST001", baud_rate=115200)
    usb = SimpleNamespace(
        device="COM7", vid=0x067B, pid=0x2303, location="1-1", serial_number=None, description="USB"
    )
    monkeypatch.setattr("serial.tools.list_ports.comports", lambda: [usb])
    # Keep all propagation, session and connection callbacks; inject only the wire data.
    app = ApplicationRuntime(root=tmp_path, hardware=False)
    inv = app.inventory
    serial = SerialDiscoveryProvider()
    visa = VisaDiscoveryProvider(SimpleNamespace(enumerate=lambda: ("ASRL7::INSTR",)))
    initial = inv.merge(serial.enumerate()[0], "old", reason="metadata_only")["id"]
    inv.merge(visa.enumerate()[0], "old", reason="metadata_only")
    saved = ConfiguredDiscoveryProvider((config,), inv)
    identity = DeviceIdentity("DUOHEYI", "DH-8318", config.expected_serial, "DEMO")
    calls = []

    def probe(candidate):
        calls.append(candidate.config)
        return identity, candidate.config.adapter

    def adapter(cfg):
        payload = f"DUOHEYI,DH-8318,{identity.serial},DEMO\n".encode()
        io = MockTransport([("*IDN?", payload), ("CH?", b"ALL\n"), ("CH?", b"ALL\n")])
        io.open()
        return DEFINITION.adapter_type(io, DEFINITION.profile, cfg)

    app.sessions.factory = adapter
    app.discovery.close()
    scan = app.discovery = DiscoveryService((saved, serial, visa), app.sessions, app.leases, inv, probe)
    try:
        for _ in range(2):
            (item,) = scan.scan().result(5)["items"]
            assert item["reason"] == "identified" and item["device_id"] == initial
            (row,) = inv.all()
            assert row["identityKey"] == identity.key
            assert row["config"]["baud_rate"] == 115200
            assert app.connect(initial, configure_acquisition=False).result(5).key == identity.key
            (item,) = scan.scan().result(5)["items"]
            assert item["reason"] == "cached_active_session" and item["device_id"] == initial
            assert len(inv.all()) == 1
            app.sessions.disconnect(initial)
        assert calls == [config, config]
        identity = DeviceIdentity("DUOHEYI", "DH-8318", "DIFFERENT", "DEMO")
        (item,) = scan.scan().result(5)["items"]
        assert item["reason"] == "probe_failed" and item["errorCode"] == "identity_conflict"
        (row,) = inv.all()
        assert row["id"] == initial and row["identity"]["serial"] == "TEST001"
        assert row["presenceState"] == "unreachable"
        assert row["candidates"]["COM7"]["verifiedScanRunId"] != row["scanRunId"]
        with pytest.raises(DeviceError) as failure:
            app.connect(initial, configure_acquisition=False).result(5)
        assert failure.value.code == "identity_conflict"
    finally:
        app.close()
