"""Verified resource associations are separate from retained address history."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from lab_control.domain.connections import ConnectionConfig, network_host
from lab_control.domain.instruments import DeviceError, DeviceIdentity


def identity_conflict_message(record: dict[str, Any]) -> str:
    conflict = record.get("identityConflict") or {}
    reason = conflict.get("reason")
    if reason == "same_identity_at_multiple_network_hosts":
        addresses = {
            network_host(address)
            for address, entry in record.get("candidates", {}).items()
            if entry.get("verifiedIdentityKey") == record.get("identityKey") and network_host(address)
        }
        detail = conflict.get("recheckDetail") or (
            "历史扫描在多个网络地址读到同一身份：" + "、".join(sorted(addresses))
            if addresses
            else "历史扫描在多个网络地址读到同一身份"
        )
        if record.get("primaryResourceExplicit"):
            detail += "；主地址由用户固定，请核对设备后手动选择"
        return f"身份冲突：{detail}"
    if reason == "configured_serial_differs":
        expected = record.get("config", {}).get("expected_serial") or "未登记"
        return f"身份冲突：配置序列号 {expected} 与实机回报 {conflict.get('serial', '')} 不同"
    if conflict.get("serial"):
        return f"身份冲突：台账与新读到的设备身份不一致（{conflict.get('model', '')} / {conflict['serial']}），请核对"
    return "设备身份冲突，请在设备详情核对型号、序列号和连接地址"


def can_recheck_network_conflict(record: dict[str, Any]) -> bool:
    binding = record.get("dhcpBinding", {})
    return bool(
        record.get("identityConflict", {}).get("reason") == "same_identity_at_multiple_network_hosts"
        and record.get("identityKey")
        and binding.get("identityKey") == record["identityKey"]
        and binding.get("mac")
        and not record.get("primaryResourceExplicit")
        and record.get("config", {}).get("transport") == "visa"
        and record.get("config", {}).get("expected_serial", "") in ("", record["identity"].get("serial"))
    )


def remember_identity(record: dict[str, Any], resource: str, identity: DeviceIdentity, scan_id: str) -> None:
    # These labels are reused across sessions and cannot establish simultaneity.
    session_labels = {"connected", "manual"}
    conflict = record.get("identityConflict", {})
    if (
        conflict.get("reason") == "same_identity_at_multiple_network_hosts"
        and conflict.get("scanRunId") in session_labels
    ):
        # Repair only this legacy false positive after a fresh matching identity read.
        record.pop("identityConflict")
    entry = record["candidates"][resource]
    entry.update(verifiedIdentityKey=identity.key, verifiedScanRunId=scan_id, retired=False)
    host = network_host(resource)
    # Different simultaneous LAN hosts with the same serial require inspection.
    duplicates = [
        other
        for address, other in record["candidates"].items()
        if host
        and scan_id not in session_labels
        and network_host(address)
        and network_host(address) != host
        and other.get("verifiedIdentityKey") == identity.key
        and other.get("verifiedScanRunId") == scan_id
        and not other.get("retired")
    ]
    if duplicates:
        record["identityConflict"] = {
            "reason": "same_identity_at_multiple_network_hosts",
            "scanRunId": scan_id,
        }


def follow_verified_address(
    record: dict[str, Any], resource: str, identity: DeviceIdentity, config: ConnectionConfig
) -> None:
    original = record.get("config", {})
    if not identity.key or record.get("primaryResourceExplicit") or record.get("identityConflict"):
        return
    if original.get("expected_serial") and original["expected_serial"] != identity.serial:
        record["identityConflict"] = {"reason": "configured_serial_differs", "serial": identity.serial}
        return
    if network_host(resource) and network_host(original.get("resource", "")):
        # Retain timeout, limits, options, unit policies and other user settings.
        record["config"] = {
            **original,
            "resource": resource,
            "transport": config.transport,
            "expected_serial": identity.serial,
            "source_address": config.source_address,
        }


def verified_connection(record: dict[str, Any]) -> ConnectionConfig:
    if record.get("identityConflict") or record.get("discoveryState") == "conflict":
        raise DeviceError(identity_conflict_message(record), "identity_conflict")
    config = ConnectionConfig(**record["config"])
    candidate = record["candidates"].get(config.resource, {})
    if candidate.get("retired"):
        raise DeviceError(
            "Saved address now belongs to a different device; scan/select a verified address",
            "identity_conflict",
        )
    key = record.get("identityKey")
    if (
        network_host(config.resource)
        and not key
        and (candidate.get("source") == "lan" or "lan" in candidate.get("sources", {}))
    ):
        raise DeviceError(
            "Discovered LAN device has no reliable serial; control is unavailable", "identity_required"
        )
    aliases = tuple(
        address
        for address, entry in record["candidates"].items()
        if address != config.resource
        and key
        and entry.get("verifiedIdentityKey") == key
        and entry.get("verifiedScanRunId") == record.get("scanRunId")
        and entry.get("presenceState") == "present"
        and not entry.get("retired")
    )
    serial = record.get("identity", {}).get("serial", "") if key else ""
    return replace(config, aliases=aliases, expected_serial=config.expected_serial or serial)
