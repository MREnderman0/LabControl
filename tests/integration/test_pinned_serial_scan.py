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


def test_hotplug_follows_verified_serial_through_legacy_duplicates_and_reused_port(tmp_path, monkeypatch):
    from copy import deepcopy
    from dataclasses import replace

    from lab_control.discovery.inventory import Inventory

    cfg = ConnectionConfig("COM7", "duoheyi.dh8318", "serial", expected_serial="TEST001", baud_rate=115200)
    usb = SimpleNamespace(
        device="COM7", vid=0x067B, pid=0x2303, location="a", serial_number=None, description="USB"
    )
    monkeypatch.setattr("serial.tools.list_ports.comports", lambda: [usb])
    serial = SerialDiscoveryProvider()
    ident = DeviceIdentity("DUOHEYI", "DH-8318", cfg.expected_serial, "DEMO")
    replies = {"COM7": ident, "COM15": ident, "COM21": replace(ident, serial="OTHER")}
    opened = []
    with ApplicationRuntime(root=tmp_path, hardware=False) as app:
        app.discovery.probe = lambda candidate: (replies[candidate.config.resource], cfg.adapter)

        def factory(config):
            dev = replies[config.resource]
            opened.append((config.resource, dev.serial))
            payload = f"DUOHEYI,DH-8318,{dev.serial},DEMO\n".encode()
            wire = MockTransport([("*IDN?", payload), ("CH?", b"ALL\n"), ("CH?", b"ALL\n")])
            wire.open()
            return DEFINITION.adapter_type(wire, DEFINITION.profile, config)

        app.sessions.factory = factory
        (item,) = app.discovery.scan((ConfiguredDiscoveryProvider((cfg,)), serial)).result(5)["items"]
        owner = item["device_id"]
        app.inventory.edit(owner, display_name="bench load")
        saved = deepcopy(app.inventory.records[owner])
        ghost = deepcopy(saved)
        ghost.update(
            id="old-metadata",
            identityKey=None,
            identity={},
            slots={"general": ""},
            displayName="",
            identityConflict=saved["identity"],
        )
        ghost["candidates"]["COM7"].pop("verifiedIdentityKey")
        app.repository.save_many((ghost, saved), remove_ids=())
        # Reorder exactly like historical databases where the anonymous row is returned first.
        app.repository.save(saved)
        app.inventory = app.discovery.inventory = Inventory(app.repository)
        assert {r["id"] for r in app.inventory.all()} == {owner}
        watcher = app.serial_autoconnect
        watcher.poll(serial.enumerate())
        assert app.inventory.records[owner]["identity"]["serial"] == "TEST001"
        assert not opened and not app.connections.snapshots()
        app.connections.request(owner, configure=None).result(5)
        assert opened == [("COM7", "TEST001")]
        watcher.poll(())
        assert owner not in app.sessions.sessions
        usb.device = "COM21"
        watcher.poll(serial.enumerate())
        history_count = len(app.inventory.all())
        watcher.retry_at.clear()
        watcher.poll(serial.enumerate())
        assert len(app.inventory.all()) == history_count
        assert owner not in app.sessions.sessions and len(opened) == 1
        assert app.inventory.records[owner]["config"]["resource"] == "COM7"
        usb.device = "COM15"
        watcher.poll(serial.enumerate())
        assert app.sessions.sessions[owner].config.resource == "COM15"
        assert app.inventory.records[owner]["displayName"] == "bench load"
        assert opened[-1] == ("COM15", "TEST001")
        assert ConfiguredDiscoveryProvider((cfg,), app.inventory).enumerate()[0].config.resource == "COM15"
        watcher.poll(serial.enumerate())
        assert len(opened) == 2
        watcher.poll(())
        usb.device = "COM7"
        watcher.poll(serial.enumerate())
        assert app.sessions.sessions[owner].config.resource == "COM7"
        assert not app.leases.owner("COM15")
        app.sessions.recovery.serial_route = watcher.recovery_route
        app.sessions.recovery.serial_verified = watcher.verified_route
        from lab_control.application.acquisition_recovery import Recovery

        usb.device = "COM15"
        app.sessions.recovery.reconnect(app.sessions.sessions[owner], Recovery("unplugged", "disconnected"))
        assert app.sessions.sessions[owner].config.resource == "COM15"
        assert app.leases.owner("COM15") == owner and not app.leases.owner("COM7")
        assert app.inventory.records[owner]["config"]["resource"] == "COM15"
