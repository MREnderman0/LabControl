"""Desktop serial hotplug, using the normal discovery and connection paths."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from lab_control.discovery.providers import SerialDiscoveryProvider
from lab_control.discovery.serial_routes import policy, serial_routes
from lab_control.domain.connections import canonical_resource
from lab_control.domain.instruments import DeviceError


@dataclass
class SelectedPorts:
    ports: tuple
    name = "serial"

    def enumerate(self):
        return self.ports


class SerialAutoConnect:
    def __init__(self, runtime):
        self.runtime = runtime
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, name="serial-hotplug", daemon=True)
        self.signature = None
        self.retry_at = {}
        self.completed = set()

    def start(self):
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread.ident is not None:
            self.thread.join()

    def recovery_route(self, session):
        mode = policy(session.config.adapter)
        if mode != "instrument_serial":
            return session.config
        routes = serial_routes(SerialDiscoveryProvider().enumerate(), self.runtime.inventory.all())
        matching = [
            c.config
            for c in routes.values()
            if c.config.adapter == session.config.adapter
            and c.config.expected_serial == session.identity.serial
        ]
        current = next((c for c in matching if c.resource == session.config.resource), None)
        if current is not None:
            return session.config
        if len(matching) != 1:
            raise DeviceError("等待可核验的电子负载串口", "disconnected")
        return matching[0]

    def verified_route(self, session, config, identity):
        from uuid import uuid4

        from lab_control.discovery.types import DiscoveryCandidate

        self.runtime.inventory.merge(
            DiscoveryCandidate(config, "configured"),
            uuid4().hex,
            identity,
            config.adapter,
            reason="identified",
            session_device_id=session.device_id,
        )

    def run(self):
        while not self.stop.wait(1.5):
            try:
                self.poll(SerialDiscoveryProvider().enumerate())
            except Exception as exc:
                if getattr(exc, "code", "") not in {
                    "scan_busy",
                    "connection_batch_busy",
                    "maintenance_busy",
                    "runtime_closing",
                }:
                    self.runtime.sessions.audit.try_record("serial_autoconnect_error", error=str(exc))
                    self.stop.wait(4)

    def poll(self, ports):
        app = self.runtime
        if (
            app.discovery.is_scanning
            or app.discovery.connection_batch_active
            or app.sessions.maintenance_active
        ):
            return
        by_port = {canonical_resource(p.config.resource): p for p in ports}
        signature = {
            k: (p.config.usb_instance_id, p.descriptor.get("hwid"), p.descriptor.get("location"))
            for k, p in by_port.items()
        }
        if self.signature != signature:
            # Explicit disconnect remains respected until that physical port changes.
            self.completed.intersection_update(
                k for k in signature if self.signature and self.signature.get(k) == signature[k]
            )
            self.retry_at.clear()
            self.signature = signature
        states = {s["stableDeviceId"]: s for s in app.sessions.snapshots()}
        for state in states.values():
            if not policy(state.get("adapterId")):
                continue
            endpoint = canonical_resource(state["resource"])
            failed = (
                state.get("connectionState") == "error"
                and state.get("resourceReleaseState") != "release_unconfirmed"
            )
            if endpoint in by_port and not failed:
                continue
            if (
                state.get("recording")
                or state.get("monitoring")
                or state.get("activeTask")
                or state.get("controlPending")
            ):
                continue  # Active acquisitions own their session and recovery.
            try:
                if app.connections.owns(state["stableDeviceId"]):
                    if endpoint in by_port:
                        continue
                    cleanup = app.connections.serial_removed(state["stableDeviceId"])
                    if cleanup is None:
                        continue
                    cleanup.result()
                else:
                    app.sessions.disconnect(state["stableDeviceId"], drain_reads=True)
            except (DeviceError, KeyError):
                continue
            self.completed.discard(endpoint)
        records = app.inventory.all()
        routes = serial_routes(ports, records)
        now = time.monotonic()
        pending = []
        suppressed = app.connections.suppressed_devices()
        for key in routes:
            owner = app.leases.owner(by_port[key].config.resource)
            if owner:
                if states.get(owner, {}).get("adapterId") == routes[key].config.adapter and states[owner][
                    "connectionState"
                ] in {"connected", "busy"}:
                    self.completed.add(key)
            elif key not in self.completed and self.retry_at.get(key, 0) <= now:
                cfg = routes[key].config
                if (
                    app.device_changes.enabled
                    and policy(cfg.adapter) == "usb_bridge"
                    and not any(
                        r.get("adapterId") == cfg.adapter
                        and (r.get("serialBindingExplicit") or r.get("identity"))
                        and cfg.usb_instance_id
                        and r["config"].get("usb_instance_id") == cfg.usb_instance_id
                        for r in records
                    )
                ):
                    continue
                if any(
                    r["id"] in suppressed
                    and r.get("adapterId") == cfg.adapter
                    and (
                        (cfg.expected_serial and r["config"].get("expected_serial") == cfg.expected_serial)
                        or (cfg.usb_instance_id and r["config"].get("usb_instance_id") == cfg.usb_instance_id)
                        or canonical_resource(r["config"]["resource"]) == key
                    )
                    for r in records
                ):
                    continue
                pending.append(by_port[key])
        if not pending or self.stop.is_set():
            return
        result = app.discovery.scan(
            (SelectedPorts(tuple(pending)),), preserve_presence=True, scope="serial_hotplug"
        ).result()
        if result["state"] != "complete" or self.stop.is_set():
            return
        for item in result["items"]:
            key = canonical_resource(item["resource"])
            self.retry_at[key] = now + 30
            if item["reason"] not in {
                "identified",
                "sdk_identity_requires_explicit_connection",
                "cached_active_session",
            }:
                continue
            row = next((r for r in app.inventory.all() if r["id"] == item["device_id"]), None)
            if not row or row.get("identityConflict") or row.get("adapterId") != routes[key].config.adapter:
                continue
            if canonical_resource(row["config"]["resource"]) != key or self.stop.is_set():
                continue
            if app.connections.owns(row["id"]):
                continue
            if not app.leases.owner(item["resource"]):
                try:
                    app.connections.request(row["id"], configure=None, hotplug=True).result()
                except Exception as exc:
                    app.sessions.audit.try_record(
                        "serial_autoconnect_failed",
                        device_id=row["id"],
                        resource=item["resource"],
                        error=str(exc),
                    )
                    continue
            self.completed.add(key)
