from types import SimpleNamespace

import pytest

from lab_control.application.acquisition_recovery import Recovery, retry_delay
from lab_control.application.network_recovery import NetworkRecovery
from lab_control.application.runtime import ApplicationRuntime
from lab_control.discovery.serial_routes import add_serial_routes, serial_routes
from lab_control.discovery.types import DiscoveryCandidate
from lab_control.domain.connections import ConnectionConfig, canonical_resource
from lab_control.domain.instruments import DeviceError, DeviceIdentity


def test_empty_onboard_port_and_legacy_alias(tmp_path):
    com = DiscoveryCandidate(
        ConnectionConfig("COM1", transport="serial"), "serial", {"hwid": r"ACPI\PNP0501\0"}
    )
    visa = DiscoveryCandidate(ConnectionConfig("ASRL1::INSTR"), "visa")
    pump = ConnectionConfig("ASRL1::INSTR", "custom.optical_comb_pump")
    with ApplicationRuntime(root=tmp_path, hardware=False) as app:
        app.inventory.merge(com, "old")
        app.inventory.merge(
            DiscoveryCandidate(pump, "configured", {"serial_autoconnect": True}), "old", adapter=pump.adapter
        )
        assert not app.inventory.all()
        key = canonical_resource(com.config.resource)
        group = {key: [com, visa]}
        add_serial_routes(group, app.inventory)
        assert not group and not serial_routes([com], app.inventory.all())
        app.inventory.merge(DiscoveryCandidate(pump, "configured"), "manual", adapter=pump.adapter)
        assert serial_routes([com], app.inventory.all())[key].config.adapter == pump.adapter


def test_network_outage_deadlines(monkeypatch):
    from lab_control.application import acquisition_recovery

    monkeypatch.setattr(acquisition_recovery.time, "monotonic", lambda: 1000.0)
    recovery = Recovery("offline", "connection_failed", network=True)
    for attempt, delay in enumerate([0.2, 0.5, 1, 2, 5, 15, 30, 60, 300, 300], start=1):
        recovery.attempts = attempt
        recovery.failed(DeviceError("offline", "connection_failed"))
        assert recovery.next_at == pytest.approx(1000 + delay)
    assert retry_delay(42, network=False) == 5


def test_lease_move_and_duplicate_live_identity(tmp_path):
    first = ConnectionConfig("TCPIP0::192.0.2.10::inst0::INSTR", "rs.fph")
    second = ConnectionConfig("TCPIP0::192.0.2.11::inst0::INSTR", "rs.fph")
    ident = DeviceIdentity("Rohde&Schwarz", "FPH", "SIM-123")
    with ApplicationRuntime(root=tmp_path, hardware=False) as app:
        row = app.inventory.merge(DiscoveryCandidate(first, "configured"), "old", ident, first.adapter)
        route = NetworkRecovery(app)
        key = row["id"]
        app.inventory.records[key]["dhcpBinding"] = {"identityKey": ident.key, "mac": "00:11:22:33:44:55"}
        route.dhcp.enumerate = lambda: [
            DiscoveryCandidate(second, "dhcp_leases", {"expectedIdentityKey": ident.key})
        ]
        session = SimpleNamespace(device_id=key, config=first, identity=ident)
        assert route.route(session).resource == second.resource
        app.probe = lambda _: (ident, first.adapter)
        with pytest.raises(DeviceError, match="相同设备身份"):
            route.verify_move(session, second, ident)
        with pytest.raises(DeviceError, match="身份冲突"):
            route.route(session)


@pytest.mark.parametrize("wrong_serial", [False, True])
def test_live_recovery_changes_endpoint_only_after_identity(tmp_path, wrong_serial):
    from dataclasses import replace

    from lab_control.demo.instrument import SimulatedInstrument

    old = ConnectionConfig("TCPIP0::192.0.2.20::inst0::INSTR", "rs.fph", expected_serial="SIM-456")
    new = replace(old, resource="TCPIP0::192.0.2.21::inst0::INSTR")
    with ApplicationRuntime(root=tmp_path, hardware=False) as app:
        ident = SimulatedInstrument(old).identify()
        record = app.inventory.merge(DiscoveryCandidate(old, "lan"), "old", ident, old.adapter)
        key = record["id"]
        app.inventory.records[key]["dhcpBinding"] = {"identityKey": ident.key, "mac": "00:11:22:33:44:55"}
        route = NetworkRecovery(app)
        route.dhcp.enumerate = lambda: [
            DiscoveryCandidate(new, "dhcp_leases", {"expectedIdentityKey": ident.key})
        ]

        def offline(_candidate):
            raise DeviceError("old host unavailable", "connection_failed")

        app.probe = offline
        app.sessions.recovery.network_route = route.route
        app.sessions.recovery.verify_move = route.verify_move
        app.sessions.recovery.serial_verified = app.serial_autoconnect.verified_route
        app.sessions.factory = SimulatedInstrument
        app.sessions.connect(key, old, configure_acquisition=False).result(3)
        if wrong_serial:
            app.sessions.factory = lambda cfg: SimulatedInstrument(replace(cfg, expected_serial="DIFFERENT"))
            with pytest.raises(DeviceError) as exc:
                app.sessions.reconnect(key, allow_connected=True).result(3)
            assert exc.value.code == "identity_conflict"
            assert app.sessions.sessions[key].config.resource == old.resource
            assert not app.leases.owner(new.resource)
        else:
            app.sessions.reconnect(key, allow_connected=True).result(3)
            assert app.sessions.sessions[key].config.resource == new.resource
            assert app.inventory.all()[0]["config"]["resource"] == new.resource
            assert not app.leases.owner(old.resource)
            assert app.leases.owner(new.resource) == key
