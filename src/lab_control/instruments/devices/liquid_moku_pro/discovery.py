"""DNS-SD observations are not ownership, slot configuration, or hardware qualification."""

import ipaddress
import re
from threading import Lock
from time import sleep

from lab_control.discovery.types import DiscoveryCandidate
from lab_control.domain.connections import ConnectionConfig

from .protocol import ADAPTER, serial_id


class Discovery:
    name = "moku_mdns"

    def enumerate(self):
        from zeroconf import IPVersion, ServiceBrowser, ServiceListener, Zeroconf

        names = set()
        lock = Lock()
        kind = "_moku._tcp.local."

        class Listener(ServiceListener):
            def add_service(self, zc, type_, name):
                with lock:
                    names.add(name)

            update_service = add_service

            def remove_service(self, zc, type_, name):
                with lock:
                    names.discard(name)

        with Zeroconf(ip_version=IPVersion.All) as zc:
            browser = ServiceBrowser(zc, kind, Listener())
            try:
                sleep(1.75)
                with lock:
                    observed = sorted(names)
                rows = []
                for name in observed:
                    match = re.fullmatch(r"MokuPro-([0-9]{1,12})\." + re.escape(kind), name, re.I)
                    if not match:
                        continue
                    info = zc.get_service_info(kind, name, timeout=1000)
                    if not info:
                        continue
                    advertised = info.properties.get(b"serial") or b""
                    if (
                        not re.fullmatch(rb"[0-9]{1,12}", advertised)
                        or not int(match[1])
                        or int(advertised) != int(match[1])
                    ):
                        continue
                    serial = serial_id(match[1])
                    addresses = [ipaddress.ip_address(a) for a in info.parsed_scoped_addresses()]
                    addresses = [
                        a
                        for a in addresses
                        if not (a.is_multicast or a.is_unspecified or a.is_loopback)
                        and not (a.version == 6 and a.is_link_local and not a.scope_id)
                    ]
                    if not addresses:
                        continue
                    # With mixed IPv4 subnets a scoped link-local IPv6 is unambiguous.
                    addr = min(addresses, key=lambda a: not (a.version == 6 and a.is_link_local))
                    resource = "moku://" + (f"[{addr}]" if addr.version == 6 else str(addr))
                    rows.append(
                        DiscoveryCandidate(
                            ConnectionConfig(resource, ADAPTER, "moku", expected_serial=serial),
                            self.name,
                            {"service": name, "display_name": "Moku:Pro"},
                        )
                    )
                return tuple(rows)
            finally:
                browser.cancel()
