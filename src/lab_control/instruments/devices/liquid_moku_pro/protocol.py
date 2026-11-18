"""MokuOS 4.4 REST. Direct, bounded, no redirects, proxy or write retries."""

from __future__ import annotations

import http.client
import ipaddress
import json
import re
from urllib.parse import urlsplit

from lab_control.domain.instruments import DeviceError, DeviceIdentity, ValidationError

MODEL = "Moku:Pro"
ADAPTER = "liquid.moku_pro"
CHANNELS = (1, 2, 3, 4)


def serial_id(value):
    if not re.fullmatch(r"[0-9]{1,12}", str(value)) or int(value) == 0:
        raise ValidationError("Moku:Pro 序列号无效")
    return str(int(value))


class Rest:
    def __init__(self, resource, timeout, trace=None, *, keep_alive=False):
        url = urlsplit(resource)
        if (
            url.scheme != "moku"
            or not url.hostname
            or url.username is not None
            or url.port is not None
            or url.path
            or url.query
            or url.fragment
        ):
            raise ValidationError("需要 moku://主机 地址（IPv6 使用方括号）")
        host = url.hostname
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if not re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)*\.?", host):
                raise ValidationError("无效的 Moku 主机名") from None
        self.host, self.timeout, self.trace = host, timeout, trace
        self.key = None
        self.keep_alive = keep_alive
        self.connection: http.client.HTTPConnection | None = None

    def close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    def call(self, method, path, params=None):
        conn = self.connection or http.client.HTTPConnection(self.host, 80, timeout=self.timeout)
        self.connection = conn
        headers = {"Content-Type": "application/json"}
        if self.key:
            headers["Moku-Client-Key"] = self.key
        try:
            conn.request(method, "/api/" + path, None if params is None else json.dumps(params), headers)
            response = conn.getresponse()
            limit = 4 * 1024 * 1024
            raw = response.read(limit + 1)
            if len(raw) > limit:
                raise DeviceError("Moku 响应过大")
            if self.trace is not None and path != "moku/claim_ownership":
                self.trace.append(
                    dict(method=method, path=path, status=response.status, body=raw.decode(errors="replace"))
                )
            if response.status != 200:
                raise DeviceError(f"{path}: HTTP {response.status}")
            result = json.loads(raw)
            if not isinstance(result, dict) or result.get("success") is not True or "data" not in result:
                raise DeviceError(
                    f"{path}: {result.get('code') if isinstance(result, dict) else 'invalid response'}"
                )
            if path == "moku/claim_ownership":
                self.key = response.getheader("Moku-Client-Key") or result["data"]
                if not isinstance(self.key, str) or not self.key:
                    raise DeviceError("未取得所有权密钥；设备占用状态未知")
            return result["data"]
        except (OSError, http.client.HTTPException) as exc:
            self.close()
            raise DeviceError(
                f"{path}: {exc}；请求不会重发", "timeout" if isinstance(exc, TimeoutError) else "disconnected"
            ) from exc
        except Exception:
            self.close()
            raise
        finally:
            if not self.keep_alive:
                self.close()

    def identity(self, expected_serial):
        description = self.call("GET", "moku/describe")
        if (
            not isinstance(description, dict)
            or description.get("hardware") != MODEL
            or not re.fullmatch(r"4\.4(?:\.\d+)*", str(description.get("mokuOS")))
        ):
            raise DeviceError("非 Moku:Pro 或 MokuOS 不是 4.4，停止操作")
        serial = serial_id(self.call("GET", "moku/serial_number"))
        if not expected_serial or serial != serial_id(expected_serial):
            raise DeviceError("未确认目标序列号或与实机不匹配")
        return DeviceIdentity("Liquid Instruments", MODEL, serial, description["mokuOS"])
