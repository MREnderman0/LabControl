"""Use verified discovery results and bound DHCP leases to recover a LAN session."""

from dataclasses import dataclass, replace
from uuid import uuid4

from lab_control.discovery.addresses import verified_connection
from lab_control.discovery.dhcp import DhcpLeaseDiscoveryProvider
from lab_control.discovery.types import DiscoveryCandidate
from lab_control.domain.connections import ConnectionConfig, network_host
from lab_control.domain.instruments import DeviceError


@dataclass
class RecoveryAddresses:
    candidates: tuple
    name = "recovery_addresses"

    def enumerate(self):
        return self.candidates


class NetworkRecovery:
    def __init__(self, runtime):
        self.runtime = runtime
        self.dhcp = DhcpLeaseDiscoveryProvider(runtime.inventory, runtime.lan_settings)

    def target(self, record, current):
        if (
            not record.get("identityKey")
            or not record.get("identity", {}).get("serial")
            or record.get("primaryResourceExplicit")
            or current.transport != "visa"
            or not network_host(current.resource)
        ):
            return None
        config = verified_connection(record)
        entry = record["candidates"].get(config.resource, {})
        verified = (
            network_host(config.resource) != network_host(current.resource)
            and entry.get("presenceState") == "present"
            and entry.get("verifiedIdentityKey") == record["identityKey"]
            and entry.get("verifiedScanRunId") == entry.get("evidenceScanRunId")
        )
        targets = {config.resource} if verified else set()
        if record.get("dhcpBinding", {}).get("identityKey") == record["identityKey"]:
            targets.update(
                c.config.resource
                for c in self.dhcp.enumerate()
                if c.descriptor.get("expectedIdentityKey") == record["identityKey"]
            )
        if len({network_host(address) for address in targets}) > 1:
            raise DeviceError("发现结果与当前租约的地址不一致，请重新扫描核对", "identity_conflict")
        return next((r for r in targets if network_host(r) != network_host(current.resource)), None)

    def route(self, session):
        record = self.runtime.inventory.by_resource(session.config.resource)
        if not record or record["id"] != session.device_id:
            record = next((r for r in self.runtime.inventory.all() if r["id"] == session.device_id), None)
        if not record or not session.identity or record.get("identityKey") != session.identity.key:
            return session.config
        target = self.target(record, session.config)
        return replace(session.config, resource=target, aliases=()) if target else session.config

    @staticmethod
    def candidate(resource, timeout):
        return DiscoveryCandidate(ConnectionConfig(resource, timeout=timeout), "lan", safe_scpi=True)

    def verify_move(self, session, config, identity):
        # The worker still holds the old and new endpoint leases. The new
        # instrument has answered correctly; rule out a second live copy.
        old = self.candidate(session.config.resource, session.config.timeout)
        try:
            previous, _ = self.runtime.probe(old)
        except DeviceError as exc:
            if exc.code not in {"connection_failed", "timeout", "device_not_present", "disconnected"}:
                raise
            return
        if previous and previous.key == identity.key:
            scan = uuid4().hex
            for resource in (old.config.resource, config.resource):
                self.runtime.inventory.merge(
                    self.candidate(resource, config.timeout),
                    scan,
                    identity,
                    config.adapter,
                    reason="identified",
                    session_device_id=session.device_id,
                )
            raise DeviceError("新旧地址同时回报相同设备身份，请核对后重连", "identity_conflict")

    def refresh_unconnected(self, record):
        config = ConnectionConfig(**record["config"])
        target = self.target(record, config)
        if target is None:
            return
        # No active session here. Both identity probes use the ordinary
        # discovery admission, resource locks and simultaneous-host checks.
        key = record["identityKey"]
        old = self.candidate(config.resource, config.timeout)
        new = replace(
            self.candidate(target, config.timeout),
            source="dhcp_leases",
            descriptor={"expectedIdentityKey": key, "dhcpMac": record["dhcpBinding"]["mac"]},
        )
        report = self.runtime.discovery.scan(
            (RecoveryAddresses((old, new)),), preserve_presence=True, scope="network_recovery"
        ).result()
        if report["state"] != "complete":
            raise DeviceError("设备地址核验未完成", "connection_failed")
        for item in report["items"]:
            if item.get("errorCode") == "identity_conflict":
                raise DeviceError(item["error"], "identity_conflict")
