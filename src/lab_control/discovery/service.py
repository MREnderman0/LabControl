from __future__ import annotations

import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import replace
from typing import Any, Callable
from uuid import uuid4

from lab_control.application.leases import ResourceLeases
from lab_control.application.sessions import SessionManager
from lab_control.discovery.addresses import (
    can_recheck_network_conflict,
    identity_conflict_message,
    network_host,
)
from lab_control.discovery.admission import probe_claims
from lab_control.discovery.inventory import Inventory as Inventory
from lab_control.discovery.presence import LIVE_CONNECTIONS
from lab_control.discovery.probe_lifetime import ProbeCleanupError
from lab_control.discovery.providers import ConfiguredDiscoveryProvider as ConfiguredDiscoveryProvider
from lab_control.discovery.providers import SerialDiscoveryProvider as SerialDiscoveryProvider
from lab_control.discovery.providers import VendorSdkDiscoveryProvider as VendorSdkDiscoveryProvider
from lab_control.discovery.providers import VisaDiscoveryProvider as VisaDiscoveryProvider
from lab_control.discovery.types import DiscoveryCandidate as DiscoveryCandidate
from lab_control.discovery.types import DiscoveryProvider as DiscoveryProvider
from lab_control.domain.connections import ConnectionConfig, canonical_resource
from lab_control.domain.instruments import DeviceError, DeviceIdentity, utc_now
from lab_control.instruments.registry import DEVICE_DEFINITIONS


class DiscoveryService:
    def __init__(
        self,
        providers: tuple[DiscoveryProvider, ...],
        sessions: SessionManager,
        leases: ResourceLeases,
        inventory: Inventory,
        probe: Callable[[DiscoveryCandidate], tuple[DeviceIdentity, str]],
        *,
        probe_workers: int = 4,
        normalize_discovery_config: Callable[[ConnectionConfig, tuple[str, ...]], ConnectionConfig]
        | None = None,
        source_addresses: Callable[[], tuple[str, ...]] | None = None,
    ) -> None:
        self.providers, self.sessions, self.leases, self.inventory, self.probe = (
            providers,
            sessions,
            leases,
            inventory,
            probe,
        )
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="discovery")
        self.probe_workers = max(1, int(probe_workers))
        self.normalize_discovery_config = normalize_discovery_config
        self.source_addresses = source_addresses
        self.lock, self.cancelled = threading.Lock(), threading.Event()
        self.future: Future[Any] | None = None
        self.progress: dict[str, Any] = {"state": "idle"}
        # Protected by the same lock as scan admission; checked by every frontend.
        self.connection_batch_active = False
        self.closing = False
        self.unreleased_probes: dict[str, ProbeCleanupError] = {}

    @property
    def is_scanning(self) -> bool:
        with self.lock:
            return bool(self.future and not self.future.done())

    def scan(
        self,
        providers: tuple[DiscoveryProvider, ...] | None = None,
        *,
        preserve_presence: bool = False,
        scope: str = "",
    ) -> Future[dict[str, Any]]:
        with self.lock:
            if self.sessions.maintenance_active:
                raise DeviceError("安装向导运行期间暂停扫描", "maintenance_busy")
            if self.closing:
                raise DeviceError("程序正在退出", "runtime_closing")
            if self.connection_batch_active:
                raise DeviceError("正在批量连接，请等待完成或停止连接", "connection_batch_busy")
            if self.future and not self.future.done():
                raise DeviceError("A scan is already running", "scan_busy")
            self.cancelled.clear()
            selected = self.providers if providers is None else tuple(providers)
            self.future = self.worker.submit(self._scan, selected, preserve_presence, scope)
            return self.future

    def cancel(self, scan_id: str = "") -> None:
        self.cancelled.set()

    def _scan(
        self,
        providers: tuple[DiscoveryProvider, ...],
        preserve_presence: bool = False,
        scope: str = "",
    ) -> dict[str, Any]:
        started = time.monotonic()
        scan_id = uuid4().hex
        report: dict[str, Any] = {
            "id": scan_id,
            "startedAt": utc_now(),
            "providers": {},
            "items": [],
            "state": "running",
        }
        if scope:
            report["scope"] = scope
        self.progress = report
        self.inventory.repository.audit("scan_started", scan_id=scan_id)
        successful: set[str] = set()
        observations: dict[str, list[DiscoveryCandidate]] = {}
        with ThreadPoolExecutor(
            max_workers=max(1, len(providers)), thread_name_prefix="enumerate"
        ) as executor:
            futures = {executor.submit(provider.enumerate): provider for provider in providers}
            for future in as_completed(futures):
                provider = futures[future]
                try:
                    result = future.result()
                    successful.add(provider.name)
                    report["providers"][provider.name] = {"count": len(result), "state": "success"}
                    for candidate in result:
                        key = canonical_resource(candidate.config.resource)
                        observations.setdefault(key, []).append(candidate)
                except Exception as exc:
                    report["providers"][provider.name] = {"state": "error", "error": str(exc)}
                self.inventory.repository.audit(
                    "provider_completed",
                    scan_id=scan_id,
                    provider=provider.name,
                    result=report["providers"][provider.name],
                )

        available_sources: tuple[str, ...] = ()
        if self.source_addresses is not None:
            try:
                available_sources = tuple(self.source_addresses())
            except DeviceError:
                # The model normalizer will publish a precise source-address
                # diagnostic for targeted endpoints; discovery remains usable
                # for serial, VISA and SDK candidates.
                available_sources = ()

        # Publish every resource before identity I/O. One slow probe cannot hide the other resources.
        candidates: list[DiscoveryCandidate] = []
        for group in observations.values():
            # Normalize saved model endpoints before the pending inventory row
            # is written.  This keeps the row's connection config identical to
            # the config that will be probed (K+K address-list entries are
            # stored as VISA Socket text but use the official-library
            # transport).  The later normalization below remains necessary
            # after source-address binding has changed a candidate.
            normalized_group = []
            for candidate in group:
                if (
                    self.normalize_discovery_config is not None
                    and self.source_addresses is not None
                    and candidate.source == "static_addresses"
                    and candidate.config.adapter
                ):
                    try:
                        candidate = replace(
                            candidate,
                            config=self.normalize_discovery_config(candidate.config, available_sources),
                        )
                    except DeviceError as exc:
                        candidate = replace(candidate, error=str(exc), error_code=exc.code)
                normalized_group.append(candidate)
            group = normalized_group
            # Explicit connection settings win, independently of provider completion order.
            chosen = max(group, key=lambda c: (bool(c.config.adapter), c.source == "configured"))
            # USB bridges without a serial number only identify a COM endpoint.  An
            # explicit instrument serial pins the intended asset for this scan; only
            # the later identity response (or its active session) confirms presence.
            serial_binding = None
            if (
                chosen.source == "configured"
                and chosen.config.transport == "serial"
                and chosen.config.adapter
                and chosen.config.expected_serial
                and not any(c.config.usb_instance_id for c in group)
            ):
                serial_binding = chosen.config
            for candidate in group:
                self.inventory.merge(
                    candidate, scan_id, reason="pending_probe", serial_binding=serial_binding
                )
            dhcp = [c for c in group if c.source == "dhcp_leases"]
            if dhcp:
                # Retain the expected identity even when a configured/remembered
                # source advertises the same resource. Probe with no old settings.
                chosen = dhcp[0]
                if len({c.descriptor["expectedIdentityKey"] for c in dhcp}) != 1:
                    chosen = replace(
                        chosen, error="DHCP endpoint maps to multiple assets", error_code="identity_conflict"
                    )
            # The saved address may pin an interface while an older startup
            # config still supplies serial/limits. Keep those identity guards,
            # but use the explicitly selected source for this exact socket.
            bound = next(
                (c for c in group if c.source == "static_addresses" and c.config.source_address), None
            )
            if bound is not None and chosen.config.transport in {"visa", "tcp"}:
                chosen = replace(
                    chosen,
                    config=replace(
                        chosen.config, transport="tcp", source_address=bound.config.source_address
                    ),
                )
            if (
                self.normalize_discovery_config is not None
                and self.source_addresses is not None
                and any(c.source == "static_addresses" for c in group)
                and chosen.config.adapter
            ):
                try:
                    chosen = replace(
                        chosen,
                        config=self.normalize_discovery_config(chosen.config, available_sources),
                    )
                except DeviceError as exc:
                    chosen = replace(chosen, error=str(exc), error_code=exc.code)
            chosen = replace(chosen, safe_scpi=any(c.safe_scpi for c in group))
            observation_error = next((c for c in group if c.error_code), None)
            if observation_error is not None and not chosen.error_code:
                chosen = replace(
                    chosen, error_code=observation_error.error_code, error=observation_error.error
                )
            serial_candidate = next((c for c in group if c.source == "serial"), None)
            if serial_candidate is not None:
                known = self.inventory.by_stable_id(serial_candidate.config.usb_instance_id)
                profile = (
                    ConnectionConfig(**known["config"])
                    if known and known.get("adapterId") and known.get("config")
                    else None
                )
                if profile is None:
                    profile = (
                        self.inventory.serial_profile(serial_candidate.descriptor)
                        if not chosen.config.adapter
                        else None
                    )
                if profile is None and not chosen.config.adapter:
                    # A USB match nominates a protocol only. The session verifies the
                    # instrument reply before exposing any controls.
                    matches = [
                        d
                        for d in DEVICE_DEFINITIONS.values()
                        if d.manual_usb_id is not None
                        and not d.manual_connection_only
                        and d.matches_manual_port(serial_candidate.descriptor)
                    ]
                    if len(matches) == 1:
                        profile = replace(serial_candidate.config, adapter=matches[0].adapter_id)
                if profile is not None and profile.adapter:
                    # Probe the currently enumerated COM resource with the
                    # confirmed model profile. The USB bridge identity, not
                    # its transient COM number, selects the profile.
                    chosen = replace(
                        serial_candidate,
                        config=replace(
                            profile,
                            resource=serial_candidate.config.resource,
                            transport="serial",
                            aliases=(),
                            usb_instance_id=serial_candidate.config.usb_instance_id,
                        ),
                        safe_scpi=any(c.safe_scpi for c in group),
                    )
            known = self.inventory.by_resource(chosen.config.resource)
            if (
                chosen.safe_scpi
                and network_host(chosen.config.resource)
                and not any(c.source == "configured" for c in group)
                and known
                and known.get("identityKey")
                and not known.get("identityConflict")
                and known["candidates"][chosen.config.resource].get("verifiedIdentityKey")
                == known["identityKey"]
            ):
                # Rediscovery must retain the verified address's connection timeout.
                # Copy no model, serial or limits: DHCP may have reassigned this address.
                chosen = replace(chosen, config=replace(chosen.config, timeout=known["config"]["timeout"]))
            candidates.append(chosen)

        # Each resource is leased atomically before opening a short-lived probe handle.
        def process(candidate: DiscoveryCandidate) -> dict[str, Any]:
            if self.cancelled.is_set():
                return {"resource": candidate.config.resource, "reason": "cancelled"}
            identity, adapter, error, reason = None, candidate.config.adapter, "", "metadata_only"
            error_code = ""
            admission_basis = ""
            blocking_owners: set[str] = set()
            owner = "probe:" + uuid4().hex
            leased = False
            definition = DEVICE_DEFINITIONS.get(candidate.config.adapter)
            try:
                if candidate.error_code:
                    raise DeviceError(candidate.error, candidate.error_code)
                with self.sessions.lock:
                    claims = probe_claims(candidate, self.inventory)
                    admission_basis = claims.basis
                    blockers = self.sessions.discovery_blockers() | self.unreleased_probes.keys()
                    blocking_owners = self.leases.conflicts(claims.resources)
                    active = self.leases.owner(candidate.config.resource)
                    session = self.sessions.sessions.get(active or "")
                    if claims.conflict:
                        record = self.inventory.by_resource(candidate.config.resource) or {}
                        raise DeviceError(identity_conflict_message(record), "identity_conflict")
                    if session is not None and blocking_owners <= {active}:
                        adapter = session.config.adapter
                        if session.state in LIVE_CONNECTIONS and not session.closing:
                            identity, reason = session.identity, "cached_active_session"
                        else:
                            reason = "cached_session_unconfirmed"
                    elif blocking_owners:
                        reason = "deferred_possible_alias_of_active_device"
                        admission_basis = "owned_resource_or_asset"
                    elif (
                        definition is not None
                        and definition.manual_connection_only
                        and candidate.config.transport != "mock"
                    ):
                        reason = "manual_connection_required"
                    elif (
                        definition is not None
                        and candidate.config.transport in definition.discovery_connection_only_transports
                    ):
                        reason = "sdk_identity_requires_explicit_connection"
                    elif blockers and (
                        not claims.trusted
                        or any(not self.leases.owns_resources(device_id) for device_id in blockers)
                    ):
                        blocking_owners = blockers
                        reason = "deferred_possible_alias_of_active_device"
                    elif candidate.config.adapter or candidate.safe_scpi:
                        self.leases.acquire(
                            owner,
                            (*claims.resources, "discovery:unknown_alias"),
                            identified=True,
                        )
                        leased = True
                if leased:
                    identity, adapter = self.probe(candidate)
                    reason = "identified"
                if (
                    identity is not None
                    and candidate.source == "configured"
                    and candidate.config.transport == "serial"
                    and candidate.config.expected_serial
                    and identity.serial
                    and identity.serial != candidate.config.expected_serial
                ):
                    actual = identity.serial
                    identity = None
                    raise DeviceError(
                        f"串口设备序列号 {actual} 与配置的 {candidate.config.expected_serial} 不一致",
                        "identity_conflict",
                    )
                expected = candidate.descriptor.get("expectedIdentityKey")
                if expected and identity is not None and identity.key != expected:
                    actual = identity.key or identity.raw or identity.model
                    identity, adapter = None, ""
                    raise DeviceError(
                        f"DHCP 地址 {network_host(candidate.config.resource)} 回报 {actual}，与预期身份 {expected} 不一致",
                        "identity_conflict",
                    )
            except Exception as exc:
                error, reason = str(exc), "probe_failed"
                error_code = exc.code if isinstance(exc, DeviceError) else ""
                if isinstance(exc, ProbeCleanupError):
                    with self.sessions.lock:
                        self.unreleased_probes[owner] = exc
            finally:
                if leased and owner not in self.unreleased_probes:
                    try:
                        self.leases.release(owner)
                    except Exception as exc:
                        with self.sessions.lock:
                            self.unreleased_probes[owner] = ProbeCleanupError(None, exc)
                        identity, adapter = None, ""
                        error, reason, error_code = str(exc), "probe_failed", "cleanup_failed"
            record = self.inventory.merge(
                candidate, scan_id, identity, adapter, error, reason, error_code=error_code
            )
            item = {
                "device_id": record["id"],
                "resource": candidate.config.resource,
                "probeTimeoutSeconds": candidate.config.timeout,
                "reason": reason,
                "error": error,
                "errorCode": error_code,
                "admissionBasis": admission_basis,
                "blockingOwners": sorted(blocking_owners),
            }
            if error_code == "identity_conflict" and candidate.descriptor.get("expectedIdentityKey"):
                item["expectedIdentityKey"] = candidate.descriptor["expectedIdentityKey"]
            self.inventory.repository.audit("discovery_result", scan_id=scan_id, **item)
            return item

        # Identity probes are the expensive part of a startup scan. Bound the
        # number of concurrent probes and keep result ordering deterministic.
        # Mock candidates remain serial because their fixture adapters share a
        # process-global simulation state; real resources are endpoint-locked.
        results: dict[int, dict[str, Any]] = {}
        groups: dict[str | int, list[tuple[int, DiscoveryCandidate]]] = {}
        for index, candidate in enumerate(candidates):
            if candidate.config.transport == "mock":
                results[index] = process(candidate)
                continue
            # The host lease is shared by VXI-11 and HiSLIP. Schedule them in sequence
            # so this scan never defers one protocol behind its own probe of the other.
            host = network_host(candidate.config.resource).rstrip(".")
            group_key = host
            record = self.inventory.by_resource(candidate.config.resource)
            if record and can_recheck_network_conflict(record):
                # Related addresses share the same asset claim; probing in parallel
                # would defer one of them behind the other.
                group_key = "conflict:" + record["id"]
            groups.setdefault(group_key or index, []).append((index, candidate))

        def process_group(group: list[tuple[int, DiscoveryCandidate]]) -> list[tuple[int, dict[str, Any]]]:
            return [(index, process(candidate)) for index, candidate in group]

        if groups:
            with ThreadPoolExecutor(
                max_workers=min(self.probe_workers, len(groups)), thread_name_prefix="probe"
            ) as probe_executor:
                for completed in probe_executor.map(process_group, groups.values()):
                    results.update(completed)
        report["items"] = [results[index] for index in range(len(candidates))]
        if not preserve_presence:
            self.inventory.stale(scan_id, successful - {"configured"}, cancelled=self.cancelled.is_set())
        if not preserve_presence and not scope and not self.cancelled.is_set() and "lan" in successful:
            mismatches = {
                item["expectedIdentityKey"]: item["error"]
                for item in report["items"]
                if item.get("expectedIdentityKey")
            }
            self.inventory.reconcile_network_conflicts(scan_id, self.cancelled.is_set, mismatches)
        report.update(
            {
                "state": "cancelled" if self.cancelled.is_set() else "complete",
                "finishedAt": utc_now(),
                "duration": time.monotonic() - started,
            }
        )
        self.inventory.repository.scan(report)
        self.inventory.repository.audit("scan_finished", **report)
        return report

    def close(self) -> None:
        self.cancel()
        self.worker.shutdown(wait=True)
        errors = []
        for owner, failure in tuple(self.unreleased_probes.items()):
            try:
                if failure.resource is not None:
                    failure.resource.close()
                self.leases.release(owner)
                del self.unreleased_probes[owner]
            except Exception as exc:
                errors.append(exc)
        if errors:
            for secondary in errors[1:]:
                errors[0].add_note(f"Additional probe cleanup failure: {secondary}")
            raise errors[0]
