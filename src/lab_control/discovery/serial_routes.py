"""Nominate a serial protocol from a saved binding or the enumerated interface.
The normal discovery/session worker verifies the instrument before it can be used.
"""

from __future__ import annotations

from dataclasses import replace

from lab_control.discovery.types import DiscoveryCandidate
from lab_control.domain.connections import ConnectionConfig, canonical_resource
from lab_control.instruments.registry import DEVICE_DEFINITIONS


def policy(adapter):
    definition = DEVICE_DEFINITIONS.get(adapter)
    return definition.serial_autoconnect if definition else ""


def serial_routes(ports, records):
    routes = {}
    for port in ports:
        key = canonical_resource(port.config.resource)
        hw = port.descriptor
        bridge = port.config.usb_instance_id
        signature = hw.get("vid"), hw.get("pid")
        choices = {}
        occupied = False
        for rec in records:
            adapter = rec.get("adapterId", "")
            if not adapter:
                continue
            cfg = rec.get("config", {})
            local = canonical_resource(cfg.get("resource", "")) == key
            same_bridge = bool(bridge and bridge == cfg.get("usb_instance_id"))
            rule = policy(adapter)
            if not rule:
                occupied |= local or same_bridge
                continue
            if rec.get("identityConflict") or rec.get("discoveryState") == "conflict":
                occupied |= local or same_bridge
                continue
            if rule == "instrument_serial":
                serial = rec.get("identity", {}).get("serial")
                if not serial or not rec.get("identityKey") or serial != cfg.get("expected_serial"):
                    continue
                fingerprints = set()
                for entry in rec["candidates"].values():
                    for desc in (
                        entry.get("rawDescriptor", {}),
                        entry.get("sources", {}).get("serial", {}).get("rawDescriptor", {}),
                    ):
                        if desc.get("vid") is not None and desc.get("pid") is not None:
                            fingerprints.add((desc["vid"], desc["pid"]))
                if fingerprints and signature not in fingerprints and not same_bridge:
                    continue
                if not (local or same_bridge or signature in fingerprints):
                    continue
            elif rule == "usb_bridge":
                if not DEVICE_DEFINITIONS[adapter].matches_manual_port(hw):
                    continue
            elif not (local or same_bridge):
                continue
            choices[(adapter, cfg.get("expected_serial", ""))] = replace(
                ConnectionConfig(**cfg),
                resource=port.config.resource,
                transport="serial",
                usb_instance_id=bridge,
                aliases=(),
            )
        if occupied:
            continue
        if not choices:
            for adapter, definition in DEVICE_DEFINITIONS.items():
                mode = definition.serial_autoconnect
                if (mode == "usb_bridge" and definition.matches_manual_port(hw)) or (
                    mode == "protocol" and str(hw.get("hwid", "")).upper().startswith("ACPI\\PNP0501")
                ):
                    choices[(adapter, "")] = replace(port.config, adapter=adapter, baud_rate=115200)
        if len(choices) == 1:
            cfg = next(iter(choices.values()))
            routes[key] = DiscoveryCandidate(cfg, "configured", {**hw, "serial_autoconnect": True})
    return routes


def add_serial_routes(observations, inventory):
    ports = [c for group in observations.values() for c in group if c.source == "serial"]
    if not ports:
        return
    records = inventory.all()
    routes = serial_routes(ports, records)
    saved = {
        (r["adapterId"], r["identity"]["serial"])
        for r in records
        if r.get("identityKey") and policy(r.get("adapterId")) == "instrument_serial"
    }
    for port in ports:
        key = canonical_resource(port.config.resource)
        group = observations[key]
        route = routes.get(key)
        # A saved COM number may already have been given to another class of instrument.
        # Only the very first identity read is allowed to rely on the operator's initial binding.
        group[:] = [
            c
            for c in group
            if c.source != "configured"
            or (c.config.adapter, c.config.expected_serial) not in saved
            or (
                route
                and route.config.adapter == c.config.adapter
                and route.config.expected_serial == c.config.expected_serial
            )
        ]
    for key, candidate in routes.items():
        group = observations[key]
        if not any(c.source == "configured" and c.config.adapter for c in group):
            group.append(candidate)
