"""KT600 manual §13.5–13.6. Only Modbus function 04 is available here."""

import re
import struct
from collections.abc import Callable
from dataclasses import dataclass, replace
from urllib.parse import urlsplit

from lab_control.domain.connections import ConnectionConfig
from lab_control.domain.instruments import DeviceError, ValidationError
from lab_control.transport.streams import TcpSocketTransport

CHANNELS = tuple(range(1, 17))
SOURCE = "https://en.pangu.com.hk/download/manual/KT600_EN.pdf"
ORDERS = {"ABCD": (0, 1, 2, 3), "BADC": (1, 0, 3, 2), "CDAB": (2, 3, 0, 1), "DCBA": (3, 2, 1, 0)}


@dataclass(frozen=True)
class Settings:
    station: int
    channels: tuple[int, ...]
    order: str
    unit: str

    @classmethod
    def from_config(cls, config: ConnectionConfig) -> "Settings":
        options: dict[str, str] = {}
        for option in config.options:
            name, sep, value = option.partition("=")
            if (
                not sep
                or not value
                or name not in {"station", "channels", "float_order", "unit"}
                or name in options
            ):
                raise ValidationError(f"Invalid KT616R option: {option}")
            options[name] = value
        try:
            station = int(options.get("station", "1"))
            channels = (
                tuple(int(ch) for ch in options["channels"].split(",")) if "channels" in options else CHANNELS
            )
        except ValueError as exc:
            raise ValidationError("Station and channel numbers must be integers") from exc
        if (
            not 1 <= station <= 247
            or not channels
            or len(set(channels)) != len(channels)
            or not set(channels) <= set(CHANNELS)
        ):
            raise ValidationError("Station must be 1–247. Select distinct channels 1–16")
        order, unit = options.get("float_order", ""), options.get("unit", "")
        if order and order not in ORDERS:
            raise ValidationError("float_order must be ABCD, BADC, CDAB or DCBA")
        if unit and not order:
            raise ValidationError("Engineering units require an explicitly configured float_order")
        return cls(station, channels, order, unit)


class RecorderTransport(TcpSocketTransport):
    """Length-delimited MBAP frames. Discard the stream after uncertain framing."""

    transaction = 0
    trace: Callable[[str, bytes], None] | None = None

    def inputs(self, station: int, offset: int, count: int) -> bytes:
        with self.lock:
            self.transaction = (self.transaction + 1) & 0xFFFF
            request = struct.pack(">HHHBBHH", self.transaction, 0, 6, station, 4, offset, count)
            if self.trace:
                self.trace("request", request)
            try:
                self.write(request)
                header = self._read_exact(7)
                if self.trace:
                    self.trace("header", header)
                tid, protocol, length, unit = struct.unpack(">HHHB", header)
                if (tid, protocol, unit) != (self.transaction, 0, station) or length not in {
                    3,
                    3 + 2 * count,
                }:
                    raise DeviceError("KT616R Modbus header mismatch", "parse_error")
                pdu = self._read_exact(length - 1)
                if self.trace:
                    self.trace("response", pdu)
                if pdu[:2] != bytes([4, 2 * count]) or len(pdu) != 2 + 2 * count:
                    if len(pdu) == 2 and pdu[0] == 0x84:
                        raise DeviceError(f"Modbus FC04 exception {pdu[1]:02X}", "modbus_exception")
                    raise DeviceError("KT616R Modbus function/byte count mismatch", "parse_error")
                return pdu[2:]
            except Exception:
                self.close()
                raise


def discovery_config(config: ConnectionConfig, _sources: tuple[str, ...]) -> ConnectionConfig:
    # The static-address dialog uses VISA SOCKET resource syntax even for raw TCP.
    if config.transport == "visa" and re.fullmatch(r"TCPIP\d*::[^:]+::\d+::SOCKET", config.resource, re.I):
        return replace(config, transport="tcp")
    return config


def create_transport(config: ConnectionConfig, _runtime: object) -> RecorderTransport:
    Settings.from_config(config)
    resource = config.resource
    match = re.fullmatch(r"TCPIP\d*::([^:]+)::(\d+)::SOCKET", resource, re.I)
    if match:
        resource = f"tcp://{match[1]}:{match[2]}"
    endpoint = urlsplit(resource)
    if (
        config.transport != "tcp"
        or endpoint.scheme != "tcp"
        or not endpoint.hostname
        or endpoint.path not in ("", "/")
        or endpoint.query
        or endpoint.fragment
        or endpoint.username is not None
    ):
        raise ValidationError("Use tcp://host:port for the selected recorder")
    port = endpoint.port if endpoint.port is not None else 502
    if not 1 <= port <= 65535:
        raise ValidationError("Invalid TCP port")
    return RecorderTransport(endpoint.hostname, port, config.timeout, source_address=config.source_address)
