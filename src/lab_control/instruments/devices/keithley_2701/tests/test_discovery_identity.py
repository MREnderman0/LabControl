from dataclasses import replace

import pytest

from lab_control.application.runtime import ApplicationRuntime
from lab_control.discovery.addresses import verified_connection
from lab_control.discovery.lan import LanDiscoverySettings, RememberedLanDiscoveryProvider
from lab_control.discovery.presence import is_current
from lab_control.discovery.types import DiscoveryCandidate
from lab_control.domain.connections import ConnectionConfig
from lab_control.domain.instruments import DeviceError
from lab_control.instruments.base import identity_from_scpi
from lab_control.instruments.registry import build_scpi_adapter, recognized_identity
from lab_control.transport.base import MockTransport

IDN = b"KEITHLEY INSTRUMENTS INC.,MODEL 2701,FIXTURE,A09  /A02"
ADDRESS = "TCPIP0::192.0.2.10::1394::SOCKET"
MODEL = "keithley.2701"


@pytest.mark.parametrize("raw", [IDN, b"+" + IDN, b"*IDN +" + IDN])
def test_scan_and_connection_agree_on_identity_and_keep_raw_reply(raw):
    wire = MockTransport([("*IDN?", raw)])
    wire.open()
    adapter = build_scpi_adapter(ConnectionConfig("mock://meter", MODEL, "mock"), wire)
    identity, model = recognized_identity(identity_from_scpi(raw))
    assert identity == adapter.identify()
    assert identity.key == identity_from_scpi(IDN).key
    assert model == MODEL and identity.raw == raw.decode()


@pytest.mark.parametrize("reply_serial", ["FIXTURE", "OTHER"])
def test_legacy_split_recovers_original_slot_only_after_live_serial_match(
    tmp_path, monkeypatch, reply_serial
):
    config = ConnectionConfig(ADDRESS, MODEL, expected_serial="FIXTURE")
    known = DiscoveryCandidate(config, "configured")
    candidate = DiscoveryCandidate(ConnectionConfig(ADDRESS), "remembered_lan", safe_scpi=True)
    with ApplicationRuntime(root=tmp_path, hardware=False) as app:
        original = app.inventory.merge(known, "connected", identity_from_scpi(IDN), MODEL)
        uid = original["id"]
        app.inventory.edit(uid, display_name="Temperature rack", slot="multimeter:1")
        legacy_id = identity_from_scpi(b"+" + IDN)
        split = app.inventory.merge(candidate, "old-scan", legacy_id, reason="identified")
        assert split["id"] != uid and not split["config"]["adapter"]

    with ApplicationRuntime(root=tmp_path, hardware=False) as app:
        lan = RememberedLanDiscoveryProvider(
            app.inventory, LanDiscoverySettings(enabled=True, networks=("192.0.2.0/24",))
        )
        (target,) = lan.enumerate()
        assert target.config.resource == ADDRESS and not target.config.adapter
        assert not is_current(app.inventory.records[uid])
        with pytest.raises(DeviceError, match="different device"):
            verified_connection(app.inventory.records[uid])
        raw = b"+" + IDN.replace(b"FIXTURE", reply_serial.encode())
        monkeypatch.setattr(
            "lab_control.application.runtime.transport_for", lambda *args: MockTransport([("*IDN?", raw)])
        )
        identity, model = app.probe(target)
        online = app.inventory.merge(target, "new-scan", identity, model, reason="identified")
        assert is_current(online) and not is_current(app.inventory.records[split["id"]])
        assert (online["id"] == uid) == (reply_serial == "FIXTURE")
        original = app.inventory.records[uid]
        assert (
            original["slots"]["general"] == "multimeter:1" and original["displayName"] == "Temperature rack"
        )
        if reply_serial == "FIXTURE":
            assert verified_connection(original).expected_serial == "FIXTURE"
            for raw_reply in (IDN, b"+" + IDN):
                (again,) = lan.enumerate()
                monkeypatch.setattr(
                    "lab_control.application.runtime.transport_for",
                    lambda *args, reply=raw_reply: MockTransport([("*IDN?", reply)]),
                )
                result = app.inventory.merge(again, "repeat", *app.probe(again), reason="identified")
                assert result["id"] == uid
                assert [row["id"] for row in app.inventory.all() if is_current(row)] == [uid]
        else:
            with pytest.raises(DeviceError, match="different device"):
                verified_connection(original)


def test_plus_is_not_stripped_from_other_manufacturers_or_models():
    original = identity_from_scpi(b"+" + IDN)
    for item in (
        replace(original, manufacturer="++KEITHLEY INSTRUMENTS INC."),
        replace(original, model="1234"),
    ):
        assert recognized_identity(item)[0] == item
