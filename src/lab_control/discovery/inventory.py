from __future__ import annotations

import threading
from copy import deepcopy
from dataclasses import asdict, replace
from typing import Any, Callable
from uuid import uuid4

from lab_control.discovery.addresses import (
    can_recheck_network_conflict,
    follow_verified_address,
    identity_conflict_message,
    network_host,
    remember_identity,
)
from lab_control.discovery.presence import local_enumeration, update_record_presence
from lab_control.discovery.types import DiscoveryCandidate
from lab_control.domain.connections import ConnectionConfig, canonical_resource
from lab_control.domain.instruments import DeviceError, DeviceIdentity, jsonable, utc_now
from lab_control.domain.workspace import GENERAL, WORKSPACE_MODES
from lab_control.instruments.registry import DEVICE_DEFINITIONS
from lab_control.storage.repository import Repository


def _promote_display_name(record: dict[str, Any]) -> bool:
    if not record.get("identityKey") or record.get("discoveryState") == "conflict":
        return False
    definition = DEVICE_DEFINITIONS.get(record.get("adapterId", ""))
    if (
        definition is None
        or not definition.pending_display_name
        or record.get("displayName") != definition.pending_display_name
    ):
        return False
    record["displayName"] = definition.documentation.title
    return True


class Inventory:
    def __init__(self, repository: Repository) -> None:
        self.repository = repository
        self.lock = threading.RLock()
        self.records = {item["id"]: item for item in repository.records()}
        self._merge_serial_placeholders()
        self._merge_legacy_visa_spellings()
        renamed = tuple(record for record in self.records.values() if _promote_display_name(record))
        if renamed:
            repository.save_many(renamed)
        # Persisted evidence describes a previous process, not currently attached hardware.
        # Reset presence in memory only.
        for record in self.records.values():
            for candidate in record["candidates"].values():
                candidate["presenceState"] = "unknown"
                candidate.pop("evidenceScanRunId", None)
            update_record_presence(record)

    def _merge_serial_placeholders(self) -> None:
        for asset in tuple(self.records.values()):
            cfg = asset.get("config", {})
            definition = DEVICE_DEFINITIONS.get(asset.get("adapterId", ""))
            key = asset.get("identityKey")
            if not (definition and definition.serial_autoconnect == "instrument_serial" and key):
                continue
            port = cfg.get("resource", "")
            if asset.get("identityConflict") or cfg.get("expected_serial") != asset["identity"].get("serial"):
                continue
            if asset["candidates"].get(port, {}).get("verifiedIdentityKey") != key:
                continue
            endpoint = canonical_resource(port)
            owners = [
                r
                for r in self.records.values()
                if r.get("identityKey")
                and any(
                    canonical_resource(a) == endpoint and not c.get("retired")
                    for a, c in r["candidates"].items()
                )
            ]
            if len(owners) != 1:
                continue
            removed = []
            for row in self.records.values():
                if (
                    row is asset
                    or row.get("identity")
                    or row.get("identityKey")
                    or row.get("displayName")
                    or any(row.get("slots", {}).values())
                ):
                    continue
                if row.get("primaryResourceExplicit") or row.get("adapterId") not in ("", asset["adapterId"]):
                    continue
                if not row["candidates"] or any(canonical_resource(a) != endpoint for a in row["candidates"]):
                    continue
                old = row.get("config", {})
                if old.get("expected_serial") not in ("", cfg["expected_serial"]) or any(
                    old.get(k) for k in ("limits", "options", "usb_instance_id", "identity_confirmed")
                ):
                    continue
                conflict = row.get("identityConflict")
                if conflict and (
                    conflict.get("reason")
                    or any(
                        conflict.get(k) != asset["identity"].get(k)
                        for k in ("manufacturer", "model", "serial")
                    )
                ):
                    continue
                removed.append(row["id"])
            if removed:
                self.repository.save_many((asset,), remove_ids=tuple(removed))
                for row_id in removed:
                    del self.records[row_id]

    def _merge_legacy_visa_spellings(self) -> None:
        """Retain the verified asset and its settings when old scans split one VISA endpoint."""
        owners: dict[str, set[str]] = {}
        for record in self.records.values():
            for address, entry in record["candidates"].items():
                if not entry.get("retired"):
                    owners.setdefault(canonical_resource(address), set()).add(record["id"])

        for record in tuple(self.records.values()):
            if len(record.get("resources", ())) != 1 or any(
                record.get(field)
                for field in (
                    "adapterId",
                    "identity",
                    "identityKey",
                    "identityConflict",
                    "slots",
                    "displayName",
                    "primaryResourceExplicit",
                )
            ):
                continue
            address = record["resources"][0]
            key = canonical_resource(address)
            entry = record["candidates"].get(address)
            if not entry:
                continue
            sources = set(entry.get("sources", {})) or {entry.get("source")}
            if not (
                key.startswith("TCPIP")
                and key.endswith("::INSTR")
                and sources <= {"visa", "lan", "remembered_lan"}
            ):
                continue
            other_ids = owners.get(key, set()) - {record["id"]}
            if len(other_ids) != 1 or entry.get("retired"):
                continue
            target = self.records[next(iter(other_ids))]
            if (
                not target.get("identityKey")
                or target.get("identityConflict")
                or address in target["candidates"]
            ):
                continue
            if not any(
                canonical_resource(resource) == key
                and not candidate.get("retired")
                and candidate.get("verifiedIdentityKey") == target["identityKey"]
                for resource, candidate in target["candidates"].items()
            ):
                continue
            target["resources"].append(address)
            target["candidates"][address] = entry
            self.repository.save_many((target,), remove_ids=(record["id"],))
            owners[key].discard(record["id"])
            del self.records[record["id"]]

    def all(self) -> tuple[dict[str, Any], ...]:
        import copy

        from lab_control.discovery.serial_routes import empty_onboard_record

        with self.lock:
            return tuple(
                copy.deepcopy(item) for item in self.records.values() if not empty_onboard_record(item)
            )

    def by_resource(self, resource: str) -> dict[str, Any] | None:
        normalized = canonical_resource(resource)
        with self.lock:
            fallback = None
            for item in self.records.values():
                for address in item["resources"]:
                    if item["candidates"].get(address, {}).get("retired"):
                        continue
                    if address == resource:
                        return item
                    if fallback is None and canonical_resource(address) == normalized:
                        fallback = item
            return fallback

    def by_stable_id(self, usb_instance_id: str) -> dict[str, Any] | None:
        """Find a serial device by its USB bridge identity, independent of COM."""
        if not usb_instance_id:
            return None
        with self.lock:
            matches = [
                item
                for item in self.records.values()
                if item.get("config", {}).get("usb_instance_id") == usb_instance_id
                or any(
                    entry.get("config", {}).get("usb_instance_id") == usb_instance_id
                    or entry.get("rawDescriptor", {}).get("usb_instance_id") == usb_instance_id
                    for entry in item.get("candidates", {}).values()
                )
            ]
            # Prefer an already configured/identified asset when legacy rows
            # and the current stable USB identity coexist in the inventory.
            return max(
                matches,
                key=lambda item: (bool(item.get("adapterId")), bool(item.get("identityKey"))),
                default=None,
            )

    @staticmethod
    def _serial_signature(descriptor: dict[str, Any]) -> tuple[int, int] | None:
        vid, pid = descriptor.get("vid"), descriptor.get("pid")
        return (vid, pid) if isinstance(vid, int) and isinstance(pid, int) else None

    def serial_profile(self, descriptor: dict[str, Any]) -> ConnectionConfig | None:
        """Return an unambiguous configured serial profile for a USB signature.

        This is opt-in through an already configured model. It lets another
        identical bridge be probed after insertion without treating its COM
        number as the device identity.
        """
        from lab_control.instruments.registry import requires_manual_connection

        signature = self._serial_signature(descriptor)
        if signature is None or not descriptor.get("usb_serial"):
            return None
        matches: dict[tuple[str, str], ConnectionConfig] = {}
        with self.lock:
            for record in self.records.values():
                if not record.get("adapterId"):
                    continue
                try:
                    config = ConnectionConfig(**record["config"])
                except (KeyError, TypeError, ValueError):
                    continue
                if config.transport != "serial" or requires_manual_connection(config.adapter):
                    continue
                if any(
                    self._serial_signature(entry.get("rawDescriptor", {})) == signature
                    for entry in record.get("candidates", {}).values()
                ):
                    matches[(config.adapter, config.transport)] = config
        if len(matches) != 1:
            return None
        profile = next(iter(matches.values()))
        # A bridge signature selects only wire defaults. Never copy another
        # asset's serial, aliases, limits, options or secondary data endpoint.
        return ConnectionConfig(
            profile.resource,
            profile.adapter,
            profile.transport,
            baud_rate=profile.baud_rate,
            timeout=profile.timeout,
            terminator=profile.terminator,
        )

    def merge(
        self,
        candidate: DiscoveryCandidate,
        scan_id: str,
        identity: DeviceIdentity | None = None,
        adapter: str = "",
        error: str = "",
        reason: str = "",
        *,
        error_code: str = "",
        serial_binding: ConnectionConfig | None = None,
        session_device_id: str = "",
    ) -> dict[str, Any]:
        from lab_control.instruments.registry import requires_manual_connection

        with self.lock:
            resource = candidate.config.resource
            stable_existing = self.by_stable_id(candidate.config.usb_instance_id)
            resource_existing = self.by_resource(resource)
            existing: dict[str, Any] | None
            if (
                serial_binding is None
                and candidate.source == "configured"
                and candidate.config.transport == "serial"
            ):
                serial_binding = candidate.config
            pinned = bool(
                serial_binding
                and serial_binding.expected_serial
                and not serial_binding.usb_instance_id
                and resource_existing
                and resource_existing.get("identityKey")
                and resource_existing.get("adapterId") == serial_binding.adapter
                and resource_existing.get("identity", {}).get("serial") == serial_binding.expected_serial
            )
            # A stable USB identity wins over a reused COM number. If an old
            # record predates this field, adopt it once at the same resource so
            # it can follow the bridge on later insertions.
            if session_device_id:
                existing = self.records[session_device_id]
            elif stable_existing is not None:
                existing = stable_existing
            elif candidate.config.usb_instance_id:
                existing = (
                    resource_existing
                    if resource_existing and not resource_existing.get("config", {}).get("usb_instance_id")
                    else None
                )
            elif pinned or (
                serial_binding
                and serial_binding.expected_serial
                and resource_existing
                and not resource_existing.get("identityKey")
                and not resource_existing.get("identityConflict")
                and resource_existing.get("adapterId") in ("", serial_binding.adapter)
                and resource_existing.get("config", {}).get("expected_serial")
                == serial_binding.expected_serial
            ):
                # A failed probe is still the same pending endpoint on the next attempt.
                # The eventual instrument reply is needed before tying it to the verified asset.
                existing = resource_existing
            elif candidate.config.transport == "serial":
                # Without a USB bridge serial there is no safe asset key. Keep
                # unconfigured metadata together for this endpoint, but do not
                # attach a configured identity to a reused COM number.
                existing = (
                    resource_existing
                    if resource_existing
                    and not resource_existing.get("identityKey")
                    and (
                        not resource_existing.get("adapterId")
                        or requires_manual_connection(resource_existing["adapterId"])
                        or (
                            (definition := DEVICE_DEFINITIONS.get(resource_existing["adapterId"])) is not None
                            and (
                                definition.serial_autoconnect == "protocol"
                                or (
                                    definition.manual_usb_id is not None
                                    and definition.matches_manual_port(candidate.descriptor)
                                )
                            )
                        )
                    )
                    else None
                )
            else:
                existing = resource_existing
            if (
                existing
                and identity
                and identity.key
                and existing.get("identityKey")
                and existing["identityKey"] != identity.key
                and network_host(resource)
                and candidate.safe_scpi
                and reason == "identified"
            ):
                # DHCP reuse does not transfer ownership, limits or historical data.
                endpoint = canonical_resource(resource)
                for address, entry in existing["candidates"].items():
                    if canonical_resource(address) == endpoint:
                        entry.update(
                            retired=True, presenceState="unreachable", probeReason="address_reassigned"
                        )
                update_record_presence(existing)
                self.repository.save(existing)
                existing = None
                candidate = replace(
                    candidate,
                    config=ConnectionConfig(
                        resource,
                        adapter,
                        candidate.config.transport,
                        timeout=candidate.config.timeout,
                        expected_serial=identity.serial,
                        source_address=candidate.config.source_address,
                    ),
                )
            matches = [
                item
                for item in self.records.values()
                if identity and identity.key and item.get("identityKey") == identity.key
            ]
            conflict = bool(
                existing
                and existing.get("identityKey")
                and identity
                and identity.key != existing["identityKey"]
            )
            if len(matches) > 1:
                conflict = True
            if existing and len(matches) == 1 and existing["id"] != matches[0]["id"]:
                if scan_id == "connected":
                    # A just-connected session owns the candidate ID. Retain both until explicit review;
                    # changing its ID here would orphan the worker and callers' handles.
                    conflict = True
                elif not existing.get("identityKey") and not conflict:
                    target = matches[0]
                    if any(
                        mode in target.get("slots", {}) and target["slots"][mode] != slot
                        for mode, slot in existing.get("slots", {}).items()
                    ):
                        conflict = True
                    else:
                        for address in existing["resources"]:
                            if address not in target["resources"]:
                                target["resources"].append(address)
                        target["candidates"].update(existing["candidates"])
                        target["displayName"] = target["displayName"] or existing["displayName"]
                        for mode, slot in existing.get("slots", {}).items():
                            target["slots"].setdefault(mode, slot)
                        self.records.pop(existing["id"])
                        self.repository.remove_inventory(existing["id"])
                        existing = target
            if existing is None and len(matches) == 1:
                existing = matches[0]
            record = existing or {
                "id": uuid4().hex,
                "resources": [],
                "candidates": {},
                "displayName": str(candidate.descriptor.get("display_name", "")),
                "slots": {},
                "identity": {},
                "identityKey": None,
                "adapterId": "",
            }
            if resource not in record["resources"]:
                record["resources"].append(resource)
            if (
                canonical_resource(resource).startswith("SERIAL:")
                and candidate.config.adapter
                and (scan_id == "manual" or candidate.descriptor.get("configured") is True)
            ):
                record["serialBindingExplicit"] = True
            if not record["displayName"] and candidate.descriptor.get("display_name"):
                record["displayName"] = str(candidate.descriptor["display_name"])
            previous = record["candidates"].get(resource, {})
            sources = dict(previous.get("sources", {}))
            if not sources and previous.get("source"):
                sources[previous["source"]] = {
                    "scanRunId": previous.get("scanRunId", ""),
                    "observedAt": previous.get("lastSeenAt", ""),
                    "rawDescriptor": previous.get("rawDescriptor", {}),
                }
            now = utc_now()
            sources[candidate.source] = {
                "scanRunId": scan_id,
                "observedAt": now,
                "rawDescriptor": candidate.descriptor,
            }
            entry = {
                **previous,
                "candidateId": previous.get("candidateId", uuid4().hex),
                "scanRunId": scan_id,
                "source": previous.get("source", candidate.source)
                if candidate.source == "configured"
                else candidate.source,
                "sources": sources,
                "rawDescriptor": candidate.descriptor,
                "lastError": error,
                "probeErrorCode": error_code,
                "probeReason": reason,
                "config": asdict(candidate.config),
            }
            local_seen = any(
                observation["scanRunId"] == scan_id and local_enumeration(source, resource)
                for source, observation in sources.items()
            )
            if local_seen and candidate.error_code in {"driver_missing", "driver_error"}:
                entry.update(presenceState="present", evidenceScanRunId=scan_id)
            elif error or reason == "probe_failed":
                entry.update(presenceState="unreachable", evidenceScanRunId=scan_id)
            elif pinned and identity is None:
                entry.update(presenceState="unknown", evidenceScanRunId=scan_id)
            elif identity is not None or local_seen:
                entry.update(presenceState="present", evidenceScanRunId=scan_id)
            elif reason != "pending_probe" and scan_id != "manual":
                entry.update(presenceState="unknown", evidenceScanRunId=scan_id)
            else:
                entry.setdefault("presenceState", "unknown")
            # A configured address alone (including failed probing) is not a sighting.
            if not error and (
                identity is not None or (not pinned and local_enumeration(candidate.source, resource))
            ):
                entry["lastSeenAt"] = now
                record["lastSeenAt"] = now
            record["candidates"][resource] = entry
            if (
                candidate.source == "serial"
                and candidate.config.usb_instance_id
                and record.get("adapterId")
                and record.get("config")
                and not record.get("identityConflict")
                and not requires_manual_connection(record["adapterId"])
            ):
                # The adapter profile follows the USB bridge; only the current
                # COM endpoint is refreshed for this scan.
                profile = ConnectionConfig(**record["config"])
                record["config"] = asdict(
                    replace(
                        profile,
                        resource=resource,
                        transport="serial",
                        aliases=(),
                        usb_instance_id=candidate.config.usb_instance_id,
                    )
                )
                record["primaryResourceExplicit"] = False
            prefer_visa = bool(
                identity is not None
                and not conflict
                and scan_id != "connected"
                and reason not in {"cached_active_session", "announced_alias"}
                and candidate.config.transport == "visa"
                and record.get("config", {}).get("transport") != "visa"
                and not record.get("primaryResourceExplicit")
                and "config" in record
            )
            if (
                "config" not in record
                or (candidate.config.adapter and not record["config"].get("adapter"))
                or scan_id == "manual"
                or prefer_visa
            ):
                record["config"] = (
                    {**record["config"], "resource": resource, "transport": "visa", "sdk_path": ""}
                    if prefer_visa
                    else asdict(candidate.config)
                )
            if identity and not conflict:
                if not identity.firmware and record.get("identity"):
                    identity = identity.with_firmware_history(DeviceIdentity(**record["identity"]))
                record["identity"], record["identityKey"] = jsonable(identity), identity.key
                if identity.key and reason not in {"cached_active_session", "announced_alias"}:
                    remember_identity(record, resource, identity, scan_id)
                if reason == "identified":
                    follow_verified_address(record, resource, identity, candidate.config)
                    if (
                        candidate.source in {"dhcp_leases", "lease_hint", "lan"}
                        and candidate.descriptor.get("expectedIdentityKey") == identity.key
                        and candidate.descriptor.get("dhcpMac")
                    ):
                        record["dhcpBinding"] = {
                            "mac": candidate.descriptor["dhcpMac"],
                            "identityKey": identity.key,
                        }
                if reason not in {"cached_active_session", "announced_alias"}:
                    record["lastProbeAt"] = now
            if adapter:
                record["adapterId"] = adapter
                record["config"]["adapter"] = adapter
            record.update(
                {
                    "discoveryState": "conflict"
                    if conflict or record.get("identityConflict")
                    else "identified"
                    if record["identity"]
                    else "unknown",
                    "scanRunId": scan_id,
                    "lastError": error,
                    "probeReason": reason,
                    "probeErrorCode": error_code,
                    "driverStatus": "error"
                    if error_code in {"driver_missing", "driver_error"}
                    else "unconfirmed"
                    if error
                    else "enumerated",
                }
            )
            if conflict:
                record["identityConflict"] = jsonable(identity)
            _promote_display_name(record)
            update_record_presence(record)
            self.records[record["id"]] = record
            self.repository.save(record)
            return record

    def edit(
        self,
        device_id: str,
        *,
        display_name: str | None = None,
        slot: str | None = None,
        mode: str = GENERAL,
    ) -> None:
        if mode not in WORKSPACE_MODES:
            raise DeviceError("Unknown workspace mode", "invalid_parameter")
        with self.lock:
            record = deepcopy(self.records[device_id])
            if display_name is not None:
                record["displayName"] = display_name.strip()[:120]
            if slot is not None:
                for other in self.records.values():
                    if slot and other["id"] != device_id and other.get("slots", {}).get(mode) == slot:
                        raise DeviceError("Slot is already occupied", "slot_conflict")
                self._set_slot(record, mode, slot)
                if mode == GENERAL:
                    record.pop("slotMigrationConflict", None)
            self.repository.save(record)
            self.records[device_id] = record

    def configure(self, device_id: str, config: ConnectionConfig) -> None:
        """Attach an explicit adapter to one discovered resource.

        Serial enumeration publishes metadata for every port.  A confirmed
        serial profile may later follow matching USB bridge signatures, while a
        port without a stable USB identity remains metadata-only.
        """
        with self.lock:
            if device_id not in self.records:
                raise DeviceError("设备记录不存在，请重新扫描", "device_not_found")
            record = deepcopy(self.records[device_id])
            normalized = canonical_resource(config.resource)
            for other in self.records.values():
                if other["id"] == device_id:
                    continue
                same_resource = any(
                    canonical_resource(resource) == normalized
                    and not other["candidates"].get(resource, {}).get("retired")
                    for resource in other["resources"]
                )
                if same_resource and (other.get("adapterId") or other.get("identityKey")):
                    raise DeviceError("该连接资源已属于另一条设备记录，请先选择主资源", "resource_conflict")
            resource = config.resource
            entry = record["candidates"].get(resource)
            if config.transport == "serial" and not config.usb_instance_id and entry:
                stable_id = entry.get("rawDescriptor", {}).get("usb_instance_id", "")
                if stable_id:
                    config = replace(config, usb_instance_id=stable_id)
            if resource not in record["resources"]:
                record["resources"].append(resource)
            if entry is None:
                entry = {
                    "candidateId": uuid4().hex,
                    "source": "configured",
                    "sources": {},
                    "rawDescriptor": {},
                    "presenceState": "unknown",
                }
            sources = dict(entry.get("sources", {}))
            sources["configured"] = {
                "scanRunId": "manual",
                "observedAt": utc_now(),
                "rawDescriptor": entry.get("rawDescriptor", {}),
            }
            entry.update(
                config=asdict(config),
                source="configured",
                sources=sources,
                probeReason="manual_configured",
                lastError="",
                probeErrorCode="",
                retired=False,
            )
            record["candidates"][resource] = entry
            record["config"] = asdict(config)
            record["adapterId"] = config.adapter
            record["discoveryState"] = "identified" if record.get("identity") else "unknown"
            record["probeReason"] = "manual_configured"
            record["lastError"] = ""
            record["probeErrorCode"] = ""
            record["driverStatus"] = "unconfirmed"
            # A serial COM number is a current endpoint, not an asset address.
            # The next scan may replace it when the same USB bridge reappears.
            record["primaryResourceExplicit"] = config.transport != "serial"
            if canonical_resource(resource).startswith("SERIAL:"):
                record["serialBindingExplicit"] = True
            update_record_presence(record)
            self.repository.save(record)
            self.records[device_id] = record

    @staticmethod
    def _set_slot(record: dict[str, Any], mode: str, slot: str) -> None:
        slots = record.setdefault("slots", {})
        if slots.get(mode, "") != slot:
            record.setdefault("slotBindingRevision", {})[mode] = uuid4().hex
        slots[mode] = slot

    def bind_slot(
        self, device_id: str, slot: str, mode: str, *, replace_device_id: str | None = None
    ) -> None:
        """Caller holds the session lock while validating both participants."""
        if mode not in WORKSPACE_MODES:
            raise DeviceError("Unknown workspace mode", "invalid_parameter")
        with self.lock:
            owners = [
                item
                for item in self.records.values()
                if slot and item["id"] != device_id and item.get("slots", {}).get(mode) == slot
            ]
            if replace_device_id is not None:
                if [item["id"] for item in owners] != [replace_device_id]:
                    raise DeviceError("槽位占用已变化，请重新选择并确认", "slot_changed")
            elif owners:
                raise DeviceError("该槽位已有设备绑定，请确认是否替换", "slot_conflict")
            record = deepcopy(self.records[device_id])
            self._set_slot(record, mode, slot)
            if mode == GENERAL:
                record.pop("slotMigrationConflict", None)
            changed = [record]
            for owner in owners:
                previous = deepcopy(owner)
                self._set_slot(previous, mode, "")
                changed.append(previous)
            self.repository.save_many(tuple(changed))
            self.records.update((item["id"], item) for item in changed)

    def select_resource(self, device_id: str, resource: str, *, follow_address: bool = False) -> None:
        with self.lock:
            record = self.records[device_id]
            if resource not in record["candidates"]:
                raise DeviceError("Resource does not belong to this inventory record", "invalid_parameter")
            entry = record["candidates"][resource]
            if entry.get("retired"):
                raise DeviceError("Address was reassigned to another device", "identity_conflict")
            serial = record.get("identity", {}).get("serial", "")
            if follow_address:
                if not network_host(resource):
                    raise DeviceError("Address following requires a LAN resource", "invalid_parameter")
                if record.get("identityConflict") or record.get("discoveryState") == "conflict":
                    raise DeviceError(identity_conflict_message(record), "identity_conflict")
                if (
                    not serial
                    or not record.get("identityKey")
                    or entry.get("verifiedIdentityKey") != record["identityKey"]
                ):
                    raise DeviceError("Verify this LAN resource and serial first", "identity_required")
                expected = record["config"].get("expected_serial")
                if expected and expected != serial:
                    raise DeviceError("Configured serial differs from asset identity", "identity_conflict")
            config = {
                **record["config"],
                "resource": resource,
                "transport": entry["config"]["transport"],
                "adapter": record["adapterId"],
            }
            if follow_address:
                config["expected_serial"] = serial
            record["config"] = config
            record["primaryResourceExplicit"] = not follow_address
            self.repository.save(record)

    def reconcile_network_conflicts(
        self, scan_id: str, cancelled: Callable[[], bool], mismatches: dict[str, str]
    ) -> None:
        from lab_control.discovery.lan import conflict_peer_macs

        for snapshot in self.all():
            if not can_recheck_network_conflict(snapshot):
                continue
            key = snapshot["identityKey"]
            peers = {
                network_host(address)
                for address, entry in snapshot["candidates"].items()
                if entry.get("verifiedIdentityKey") == key
                and not entry.get("retired")
                and network_host(address)
            }
            live = [
                address
                for address, entry in snapshot["candidates"].items()
                if network_host(address) in peers
                and entry.get("verifiedIdentityKey") == key
                and entry.get("verifiedScanRunId") == scan_id
                and entry.get("probeReason") == "identified"
                and not entry.get("retired")
            ]
            hosts = {network_host(address) for address in live}
            macs = {}
            if len(hosts) > 1:
                detail = "本轮在多个地址读到相同身份：" + "、".join(sorted(hosts))
            elif not hosts:
                detail = "本轮未从候选地址读到匹配的型号和序列号"
            else:
                host = next(iter(hosts))
                macs = conflict_peer_macs(peers)
                other = sorted(peer for peer in peers - {host} if macs.get(peer))
                if any(macs.get(peer) is None for peer in peers) or not macs.get(host):
                    detail = "同一物理网段的网卡地址证据不足，未确认先前地址已离线"
                elif macs[host] != snapshot["dhcpBinding"]["mac"]:
                    detail = f"{host} 返回的网卡地址 {macs[host]} 与资产绑定 {snapshot['dhcpBinding']['mac']} 不一致"
                elif other:
                    detail = f"{host} 返回了匹配身份，历史地址 {'、'.join(other)} 仍在线"
                else:
                    detail = ""
            if key in mismatches:
                detail = mismatches[key]
            with self.lock:
                record = self.records[snapshot["id"]]
                # The probes above did not hold the inventory lock over network I/O.
                if record != snapshot or cancelled():
                    continue
                if detail:
                    record["identityConflict"]["recheckDetail"] = detail
                    record["identityConflict"]["recheckScanRunId"] = scan_id
                    self.repository.save(record)
                    continue
                previous = record["config"]["resource"]
                address = previous if previous in live else sorted(live)[0]
                config = ConnectionConfig(**record["candidates"][address]["config"])
                del record["identityConflict"]
                follow_verified_address(record, address, DeviceIdentity(**record["identity"]), config)
                record.update(
                    discoveryState="identified", probeReason="identified", lastError="", probeErrorCode=""
                )
                update_record_presence(record)
                self.repository.save(record)
                self.repository.audit(
                    "network_identity_reconciled",
                    scan_id=scan_id,
                    device_id=record["id"],
                    resource=address,
                    previous_resource=previous,
                    peer_macs=macs,
                )

    def withdraw_observations(self, removed, seen) -> None:
        """Withdraw only vanished physical observations, retaining history and live sessions."""
        with self.lock:
            for record in self.records.values():
                dirty = False
                for address, entry in record["candidates"].items():
                    if entry.get("retired"):
                        continue
                    keys = {
                        (
                            source,
                            canonical_resource(
                                observation.get("rawDescriptor", {}).get("originalLanResource") or address
                            ),
                        )
                        for source, observation in entry.get("sources", {}).items()
                        if source == "lease_hint" or local_enumeration(source, address)
                    }
                    if keys & removed and not keys & seen:
                        entry.update(presenceState="not_seen", probeReason="not_seen")
                        dirty = True
                if dirty:
                    update_record_presence(record)
                    self.repository.save(record)

    def stale(self, scan_id: str, successful_sources: set[str], *, cancelled: bool = False) -> None:
        with self.lock:
            for record in self.records.values():
                for resource, candidate in record["candidates"].items():
                    if candidate.get("evidenceScanRunId") == scan_id:
                        continue
                    if any(
                        observation.get("scanRunId") == scan_id and local_enumeration(source, resource)
                        for source, observation in candidate.get("sources", {}).items()
                    ):
                        # A concurrent connection may have replaced the evidence ID.
                        # Do not discard this scan's physical sighting during finalization.
                        continue
                    sources = set(candidate.get("sources", {})) or {candidate.get("source", "")}
                    physical = {source for source in sources if local_enumeration(source, resource)}
                    missing = bool(physical and physical <= successful_sources and not cancelled)
                    candidate["presenceState"] = "not_seen" if missing else "unknown"
                    candidate["probeReason"] = (
                        "not_seen"
                        if missing
                        else "scan_cancelled"
                        if cancelled
                        else "enumeration_unconfirmed"
                    )
                update_record_presence(record)
                self.repository.save(record)
