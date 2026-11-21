from dataclasses import replace

import pytest

from lab_control.application.config import ConnectionConfig
from lab_control.application.runtime import ApplicationRuntime
from lab_control.discovery.addresses import verified_connection
from lab_control.discovery.service import DiscoveryCandidate
from lab_control.domain.instruments import DeviceError
from lab_control.instruments.base import identity_from_scpi
from lab_control.instruments.registry import build_scpi_adapter
from lab_control.transport.base import MockTransport


@pytest.mark.parametrize("header", [b"", b"*IDN ", b"*idn\t"])
def test_identity_response_header_is_not_part_of_the_asset_key(header):
    raw = header + b"LECROY,WAVERUNNER9404M,FIXTURE,9.2.0\r\n"
    identity = identity_from_scpi(raw)
    assert identity.key == "LECROY|WAVERUNNER9404M|FIXTURE"
    assert identity.raw == raw.decode().strip()
    assert identity_from_scpi(b"IDN Industries,MODEL,SERIAL,1").manufacturer == "IDN Industries"


@pytest.mark.parametrize(
    ("adapter_id", "idn"),
    [
        ("keysight.n9030a", b"Agilent Technologies,N9030A,FIXTURE"),
        ("siglent.sdg6032x", b"Siglent Technologies,SDG6032X,FIXTURE"),
        ("siglent.sdg2082x", b"Siglent Technologies,SDG2082X,FIXTURE"),
    ],
)
def test_verified_serial_supports_an_idn_without_the_optional_revision(adapter_id, idn):
    from lab_control.application.acquisition_recovery import identify_for_connection

    config = ConnectionConfig("mock://partial-idn", adapter_id, "mock")
    wire = MockTransport([("*IDN?", idn + b",1.2"), ("*IDN?", idn)])
    wire.open()
    adapter = build_scpi_adapter(config, wire)
    original = adapter.identify()
    partial = identify_for_connection(adapter, original)
    assert partial.key == original.key
    assert partial.firmware == "" and partial.last_known_firmware == "1.2"
    assert wire.history == ["*IDN?", "*IDN?"]


def test_lan_probe_and_explicit_connection_keep_one_asset_without_setting_writes(tmp_path, monkeypatch):
    raw = b"*IDN LECROY,WAVERUNNER9404M,FIXTURE,9.2.0"
    config = ConnectionConfig("mock://header-scope", "lecroy.9404m", "mock", expected_serial="FIXTURE")
    wires = []

    def transport(*args):
        wire = MockTransport([("*IDN?", raw)])
        wires.append(wire)
        return wire

    with ApplicationRuntime(root=tmp_path, hardware=False) as app:
        monkeypatch.setattr("lab_control.application.runtime.transport_for", transport)
        candidate = DiscoveryCandidate(replace(config, adapter=""), "lan", safe_scpi=True)
        identity, adapter = app.probe(candidate)
        # A configured candidate existed before LAN identification.
        key = app.add(config)
        scanned = app.inventory.merge(candidate, "scan-a", identity, adapter, reason="identified")
        assert scanned["id"] == key

        def factory(selected):
            wire = transport()
            wire.open()
            scope = build_scpi_adapter(selected, wire)
            from lab_control.instruments.devices.lecroy_waverunner9404m.ranges import (
                MEMORY_QUERIES,
                PATHS,
                cvar_query,
            )

            wire.exchanges.extend(
                (query, {"mode": b"SetMaximumMemory", "points": b"1000", "sample_rate": b"1e9"}[key])
                for key, query in MEMORY_QUERIES.items()
            )
            for op in PATHS:
                cap = scope.profile.commands[op].capability
                spec = cap.parameters["value"]
                for ch in scope.profile.channels if cap.channel_scoped else (1,):
                    wire.exchanges.extend(
                        (cvar_query(op, ch, member), str(value).encode())
                        for member, value in (
                            ("GetMinValue", spec.minimum),
                            ("GetMaxValue", spec.maximum),
                            ("GetGrainValue", 0),
                        )
                    )
            return scope

        app.sessions.factory = factory
        connected = app.connect(key, configure_acquisition=False).result(3)
        assert connected == identity
        # Run the same completion merge synchronously to inspect its outcome.
        current = app.inventory.merge(
            DiscoveryCandidate(config, "configured"), "connected", connected, adapter
        )
        assert len(app.inventory.all()) == 1 and not current.get("identityConflict")
        assert verified_connection(current).expected_serial == "FIXTURE"
        assert wires[0].history == ["*IDN?"]  # scan never refreshes settings/ranges
        assert all("?" in q for wire in wires for q in wire.history)
        assert not wires[-1].exchanges

        other = replace(identity, serial="DIFFERENT", raw="LECROY,WAVERUNNER9404M,DIFFERENT,9.2.0")
        conflict = app.inventory.merge(DiscoveryCandidate(config, "configured"), "connected", other, adapter)
        with pytest.raises(DeviceError) as caught:
            verified_connection(conflict)
        assert caught.value.code == "identity_conflict"
