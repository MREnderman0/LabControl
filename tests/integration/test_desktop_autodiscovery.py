"""Desktop plug-in, identity and deferred initialization with no physical I/O."""

import threading
import time
from dataclasses import replace

from lab_control.application.runtime import ApplicationRuntime
from lab_control.demo.instrument import SimulatedInstrument
from lab_control.discovery.changes import Observations
from lab_control.discovery.lxi import enrich_candidates, identify_endpoint
from lab_control.discovery.providers import VisaDiscoveryProvider
from lab_control.discovery.types import DiscoveryCandidate
from lab_control.domain.connections import ConnectionConfig
from lab_control.domain.instruments import AcquisitionRequest


def wait(predicate):
    until = time.monotonic() + 5
    while not predicate():
        assert time.monotonic() < until
        time.sleep(0.01)


def advertised(host, source="lan"):
    return DiscoveryCandidate(ConnectionConfig(f"TCPIP0::{host}::inst0::INSTR"), source, safe_scpi=True)


def metadata(serial):
    def fetch(host, path):
        if path == "/lxi/identification":
            return (
                f"<LXIDevice xmlns='urn:lxi'><Manufacturer>RIGOL TECHNOLOGIES</Manufacturer>"
                f"<Model>DHO924</Model><SerialNumber>{serial}</SerialNumber>"
                "<FirmwareRevision>1.2</FirmwareRevision></LXIDevice>"
            ).encode()
        assert path == "/welcome.html"
        return b"<tr><td>SCPI Socket Port:</td><td>5777</td></tr>"

    return identify_endpoint("192.0.2.1", fetch)


def test_first_discovery_during_recording_aliases_and_operator_disconnect(tmp_path):
    with ApplicationRuntime(root=tmp_path, hardware=False) as app:
        old = app.add(ConnectionConfig("mock://recording", "rs.rtm3004", "mock"))
        app.connect(old).result(3)
        old_session = app.sessions.sessions[old]
        request = AcquisitionRequest("waveform", (1,), 32)
        app.monitoring.start(old, (request,), interval=0.05, recording=True)
        wait(lambda: bool(app.monitoring.snapshots()[old]["last_path"]))
        first_path = app.monitoring.snapshots()[old]["last_path"]
        app.device_changes.enabled = True
        app.discovery.on_complete = app.connections.discovered
        hosts = {"192.0.2.21": "DHO-A001", "192.0.2.22": "DHO-A002"}
        http, probes, opened, setup = [], [], [], []
        pause, release = threading.Event(), threading.Event()

        def lookup(host):
            http.append(host)
            return metadata(hosts[host])

        def probe(c):
            probes.append(c.config.resource)
            return metadata(c.config.expected_serial)[:2]

        def factory(cfg):
            opened.append(cfg.resource)
            adapter = SimulatedInstrument(cfg)
            adapter.identity = metadata(cfg.expected_serial)[0]
            adapter.prepare_acquisition = lambda: setup.append(cfg.resource)
            if cfg.expected_serial == "DHO-A001":
                pause.set()
                assert release.wait(5)
            return adapter

        app.discovery.enrich_candidates = lambda candidates: enrich_candidates(candidates, lookup)
        app.discovery.probe = probe
        app.sessions.factory = factory
        lan = Observations("lan", (advertised("192.0.2.21"),))
        dhcp = Observations("dhcp_leases", (advertised("192.0.2.21", "lease_hint"),))
        app.discovery.providers = (lan, dhcp)
        try:
            app.device_changes.poll()
            assert pause.wait(3)
            target = next(r["id"] for r in app.inventory.all() if r.get("adapterId") == "rigol.dho924")
            click = app.connections.request(target)
        finally:
            release.set()
        click.result(5)
        assert http == ["192.0.2.21"]
        assert opened == probes == ["TCPIP0::192.0.2.21::5777::SOCKET"]
        app.device_changes.poll()
        assert http == ["192.0.2.21"]
        assert not setup
        passive = replace(request, allow_setup=False)
        app.sessions.submit(target, "acquire", passive).result(2)
        assert not setup
        for _ in range(2):
            app.sessions.submit(target, "acquire", request).result(2)
        assert setup == opened
        usb = "USB0::0x1AB1::0x1234::DHO-A001::INSTR"
        visa = VisaDiscoveryProvider(type("Visa", (), {"enumerate": lambda _: (usb,)})())
        unknown = DiscoveryCandidate(ConnectionConfig("COM99", transport="serial"), "serial")
        lan.items = (*lan.items, advertised("192.0.2.22"))
        app.discovery.providers = (lan, dhcp, visa, Observations("serial", (unknown,)))
        app.device_changes.poll()
        wait(lambda: len(opened) == 2 and len(app.sessions.snapshots()) == 3)
        rows = app.inventory.all()
        assert len(rows) == 4
        assert app.inventory.by_resource(usb)["id"] == target
        assert app.inventory.by_resource("COM99")["probeReason"] == "identity_needs_review"
        assert usb not in probes and "COM99" not in probes
        assert app.sessions.sessions[old] is old_session and old_session.adapter.sample_index > 0
        assert app.monitoring.snapshots()[old]["recording"]
        assert app.monitoring.snapshots()[old]["last_path"] == first_path
        app.connections.disconnect(target).result(3)
        app.discovery.scan().result(3)
        assert target in app.connections.suppressed_devices()
        assert len(opened) == 2
        app.monitoring.stop(old).result(3)


def test_lxi_and_socket_must_agree_before_auto_connect(tmp_path):
    with ApplicationRuntime(root=tmp_path, hardware=False) as app:
        app.discovery.enrich_candidates = lambda cs: enrich_candidates(cs, lambda host: metadata("DHO-A001"))
        app.discovery.probe = lambda c: metadata("DHO-A002")[:2]
        app.device_changes.enabled = True
        app.discovery.on_complete = app.connections.discovered
        observation = Observations("lan", (advertised("192.0.2.21"),))
        report = app.discovery.scan((observation,)).result(3)
        assert report["items"][0]["errorCode"] == "identity_conflict"
        assert not app.inventory.all()[0]["identityKey"]
        assert not app.sessions.snapshots()


def test_dhcp_move_preserves_asset_and_manual_alias(tmp_path):
    with ApplicationRuntime(root=tmp_path, hardware=False) as app:
        identity, adapter = metadata("DHO-A007")[:2]
        one = advertised("192.0.2.31")
        old = app.inventory.merge(one, "earlier")
        old["displayName"] = "Bench scope"
        old["slots"] = {"general": "oscilloscopes:1"}
        app.discovery.probe = lambda c: (identity, adapter)
        app.discovery.enrich_candidates = lambda cs: enrich_candidates(cs, lambda host: metadata("DHO-A007"))
        lan = Observations("lan", (one,))
        app.discovery.providers = (lan,)
        app.device_changes.poll()
        assert len(app.inventory.all()) == 1
        key = app.inventory.all()[0]["id"]
        assert key == old["id"]
        lan.items = (advertised("192.0.2.32"),)
        app.device_changes.poll()
        (row,) = app.inventory.all()
        assert row["config"]["resource"] == "TCPIP0::192.0.2.32::5777::SOCKET"
        assert (row["id"], row["displayName"], row["slots"]) == (
            key,
            "Bench scope",
            {"general": "oscilloscopes:1"},
        )
        assert row["candidates"]["TCPIP0::192.0.2.31::5777::SOCKET"]["presenceState"] == "not_seen"


def test_lease_and_static_model_conflicts_are_not_overwritten():
    c = replace(advertised("192.0.2.9", "dhcp_leases"), descriptor={"expectedIdentityKey": "prior"})
    static = replace(c, source="static_addresses", config=replace(c.config, adapter="rs.rtm3004"))
    items = enrich_candidates((c, static), lambda host: metadata("DHO-A002"))
    assert [item.error_code for item in items] == ["identity_conflict", "model_mismatch"]
