"""Desktop discovery by differences between provider enumerations.

VISA discovery consumes service advertisements. Enumeration has no unicast
address-range or port sweep. Only changed observations enter the probe worker.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from lab_control.domain.connections import canonical_resource
from lab_control.domain.instruments import DeviceError


@dataclass
class Observations:
    name: str
    items: tuple

    def enumerate(self):
        return self.items


class DiscoveryChanges:
    def __init__(self, runtime):
        self.runtime = runtime
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, name="device-discovery-events", daemon=True)
        self.lock = threading.Lock()
        self.generation = 0
        self.previous = {}
        self.enabled = False

    @staticmethod
    def snapshot(items):
        return {(c.source, canonical_resource(c.config.resource)): c for c in items}

    def observed(self, provider, items):
        with self.lock:
            self.generation += 1
            self.previous[provider] = self.snapshot(items)

    def run(self):
        while not self.stop.wait(4):
            try:
                self.poll()
            except DeviceError as exc:
                if exc.code not in {
                    "scan_busy",
                    "maintenance_busy",
                    "connection_batch_busy",
                    "runtime_closing",
                }:
                    self.runtime.repository.audit("device_discovery_error", error=str(exc))

            except Exception as exc:
                self.runtime.sessions.audit.try_record("device_discovery_error", error=str(exc))

    def poll(self):
        app = self.runtime
        if (
            app.discovery.is_scanning
            or app.discovery.connection_batch_active
            or app.sessions.maintenance_active
        ):
            return
        metadata_sources = {"configured", "static_addresses", "remembered_lan"}
        providers = [p for p in app.discovery.providers if p.name not in metadata_sources]
        batches = {}
        with self.lock:
            generation = self.generation
        with ThreadPoolExecutor(
            max_workers=max(1, len(providers)), thread_name_prefix="watch-enumerate"
        ) as pool:
            futures = {pool.submit(p.enumerate): p.name for p in providers}
            for future in as_completed(futures):
                try:
                    batches[futures[future]] = self.snapshot(future.result())
                except Exception:
                    # An unavailable provider is not evidence of device removal.
                    continue
        if self.stop.is_set():
            return
        with self.lock:
            if generation != self.generation:
                return
            removed = {
                key for name, now in batches.items() for key in self.previous.get(name, {}) if key not in now
            }
            changed = {
                key[1]
                for name, now in batches.items()
                for key, candidate in now.items()
                if self.previous.get(name, {}).get(key) != candidate
            }
        if changed:
            # Keep other observations of the same endpoint together for admission.
            selected = []
            for name, now in batches.items():
                peers = tuple(c for (_, endpoint), c in now.items() if endpoint in changed)
                if peers:
                    selected.append(Observations(name, peers))
            for provider in app.discovery.providers:
                if provider.name in {"configured", "static_addresses"}:
                    peers = tuple(
                        c for c in provider.enumerate() if canonical_resource(c.config.resource) in changed
                    )
                    if peers:
                        selected.append(Observations(provider.name, peers))
            result = app.discovery.scan(
                tuple(selected), preserve_presence=True, scope="device_changes"
            ).result()
            if result["state"] != "complete":
                return
        with self.lock:
            if generation != self.generation:
                return
            self.previous.update(batches)
            if removed:
                seen = {key for batch in self.previous.values() for key in batch}
                app.inventory.withdraw_observations(removed - seen, seen)

    def start(self):
        if not self.enabled:
            self.enabled = True
            self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread.ident is not None:
            self.thread.join()
