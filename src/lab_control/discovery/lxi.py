"""Read identity metadata only from the exact IPv4 host reported by discovery."""

from __future__ import annotations

import http.client
import ipaddress
import re
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import NamedTuple

from lab_control.domain.connections import network_host
from lab_control.domain.instruments import DeviceIdentity
from lab_control.instruments.registry import DEVICE_DEFINITIONS, recognized_identity


class EndpointIdentity(NamedTuple):
    identity: DeviceIdentity
    adapter: str
    socket_port: int | None
    visa_resource: str


def read_page(host: str, path: str) -> bytes:
    connection = http.client.HTTPConnection(str(ipaddress.IPv4Address(host)), timeout=1)
    try:
        connection.request("GET", path, headers={"Connection": "close"})
        response = connection.getresponse()
        if response.status != 200:
            raise ValueError(f"HTTP {response.status}")
        data = response.read(65537)
        if len(data) > 65536:
            raise ValueError("Instrument metadata exceeds 64 KiB")
        return data
    finally:
        connection.close()


def identify_endpoint(host, fetch=read_page):
    try:
        xml = ET.fromstring(fetch(host, "/lxi/identification"))
        if xml.tag.rsplit("}", 1)[-1] != "LXIDevice":
            return None
        values = [
            (xml.findtext("{*}" + k) or "").strip()
            for k in ("Manufacturer", "Model", "SerialNumber", "FirmwareRevision")
        ]
        identity, adapter = recognized_identity(DeviceIdentity(*values))
        if not identity.key or not adapter:
            return None
        definition = DEVICE_DEFINITIONS[adapter]
        port = None
        if definition.advertised_socket_port:
            try:
                port = definition.advertised_socket_port(host, fetch)
            except (OSError, ValueError, http.client.HTTPException):
                pass
        visa_resource = ""
        for node in xml.findall(".//{*}InstrumentAddressString"):
            parts = (node.text or "").strip().split("::")
            if len(parts) != 4 or not re.fullmatch(r"TCPIP\d*", parts[0], re.I):
                continue
            if parts[1] not in {host, "0.0.0.0"}:
                continue
            if (
                parts[3].upper() == "SOCKET"
                and parts[2].isascii()
                and parts[2].isdigit()
                and 1 <= int(parts[2]) <= 65535
            ):
                port = port or int(parts[2])
            elif parts[3].upper() == "INSTR" and re.fullmatch(r"(?:inst|hislip)\d+", parts[2], re.I):
                visa_resource = f"TCPIP0::{host}::{parts[2]}::INSTR"
        return EndpointIdentity(identity, adapter, port, visa_resource)
    except (OSError, ValueError, ET.ParseError, http.client.HTTPException):
        return None


def enrich_candidates(candidates, lookup=identify_endpoint):
    """Coalesce protocol advertisements before creating inventory rows."""
    candidates = tuple(candidates)
    hosts = tuple(
        dict.fromkeys(
            host
            for candidate in candidates
            if candidate.source in {"lan", "lease_hint", "dhcp_leases", "static_addresses"}
            and (host := network_host(candidate.config.resource))
        )
    )
    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="lxi-metadata") as pool:
        answers = dict(zip(hosts, pool.map(lookup, hosts)))
    result = []
    for candidate in candidates:
        host = network_host(candidate.config.resource)
        if candidate.source not in {"lan", "lease_hint", "dhcp_leases", "static_addresses"} or not host:
            result.append(candidate)
            continue
        answer = answers[host]
        if answer is None:
            if candidate.source != "lease_hint":
                result.append(candidate)
            continue
        identity, adapter, reported_port, declared_visa = answer
        port = reported_port or DEVICE_DEFINITIONS[adapter].socket_port
        if candidate.source == "lease_hint" and not port and not declared_visa:
            # Lease alone supplies no SCPI protocol.
            continue
        if candidate.config.adapter and candidate.config.adapter != adapter:
            result.append(
                replace(
                    candidate,
                    error=f"所选型号与仪器回报的 {identity.model} 不匹配",
                    error_code="model_mismatch",
                )
            )
            continue
        expected = candidate.descriptor.get("expectedIdentityKey")
        if expected and expected != identity.key:
            result.append(
                replace(candidate, error="租约对应的身份与仪器网页不匹配", error_code="identity_conflict")
            )
            continue
        resource = f"TCPIP0::{host}::{port}::SOCKET" if port else candidate.config.resource
        if candidate.source == "lease_hint" and not port:
            resource = declared_visa
        if candidate.source == "static_addresses":
            resource = candidate.config.resource
        result.append(
            replace(
                candidate,
                config=replace(
                    candidate.config, resource=resource, adapter=adapter, expected_serial=identity.serial
                ),
                safe_scpi=True,
                descriptor={
                    **candidate.descriptor,
                    "originalLanResource": candidate.config.resource,
                    "lxiIdentityKey": identity.key,
                    "expectedIdentityKey": identity.key,
                    "advertisedSocketPort": reported_port,
                    "verifiedModel": identity.model,
                },
            )
        )
    return result
