"""Endpoint preview and background identity lookup for the desktop address dialog."""

import ipaddress
import re

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QLabel, QLineEdit, QSpinBox

from lab_control.discovery.lxi import identify_endpoint
from lab_control.discovery.presence import identity_review_reason
from lab_control.instruments.registry import DEVICE_DEFINITIONS


def parse_endpoint(text):
    value = text.strip()
    if re.fullmatch(r"(?:COM|ASRL)\s*\d+(?:::INSTR)?", value, re.I):
        return "serial", value.upper(), None
    host, separator, suffix = value.partition(":")
    try:
        ip = ipaddress.IPv4Address(host)
        if ip.is_loopback or ip.is_unspecified or ip.is_multicast:
            raise ValueError
    except ValueError as exc:
        raise ValueError("请填写有效的仪器 IPv4 地址或 COM 口") from exc
    if separator and (not suffix.isascii() or not suffix.isdigit() or not 1 <= int(suffix) <= 65535):
        raise ValueError("Socket 端口应为 1 ～ 65535")
    return "ip", str(ip), int(suffix) if separator else None


class EndpointFields:
    def __init__(self, dialog, form, window, model, endpoint):
        self.model, self.endpoint, self.window = model, endpoint, window
        self.reported, self.pending = {}, {}
        self.custom_port = False
        self.search = QLineEdit()
        self.search.setObjectName("staticModelSearch")
        self.search.setPlaceholderText("按厂家或型号搜索")
        self.options = [(model.itemText(i), model.itemData(i)) for i in range(model.count())]
        form.addRow("搜索型号", self.search)
        form.addRow("设备型号", model)
        form.addRow("IP / COM 口", endpoint)
        self.port = QSpinBox()
        self.port.setObjectName("staticSocketPort")
        self.port.setRange(0, 65535)
        self.port.setSpecialValueText("VISA INSTR")
        form.addRow("Socket 端口", self.port)
        self.preview = QLabel()
        self.preview.setObjectName("staticEndpointPreview")
        form.addRow("连接预览", self.preview)
        self.changed = lambda: None
        self.debounce = QTimer(dialog)
        self.debounce.setSingleShot(True)
        self.poll = QTimer(dialog)
        self.poll.setInterval(100)
        self.debounce.timeout.connect(self.lookup)
        self.poll.timeout.connect(self.collect)
        endpoint.textChanged.connect(self.address_changed)
        model.currentIndexChanged.connect(self.defaults)
        self.search.textChanged.connect(self.filter)
        self.port.valueChanged.connect(self.port_changed)
        self.defaults()

    def filter(self, query):
        previous = self.model.currentData()
        self.model.blockSignals(True)
        self.model.clear()
        for name, key in self.options:
            if not key or query.casefold() in name.casefold():
                self.model.addItem(name, key)
        self.model.setCurrentIndex(max(0, self.model.findData(previous)))
        self.model.blockSignals(False)
        self.defaults()

    def input(self):
        return parse_endpoint(self.endpoint.text())

    def waiting(self):
        try:
            kind, host, _ = self.input()
        except ValueError:
            return False
        return self.window.runtime.hardware and kind == "ip" and host not in self.reported

    def port_changed(self):
        self.custom_port = True
        self.update_preview()

    def defaults(self):
        self.custom_port = False
        try:
            kind, host, suffix = self.input()
        except ValueError:
            kind, host, suffix = "ip", "", None
        adapter = self.model.currentData()
        definition = DEVICE_DEFINITIONS.get(adapter)
        number = definition.socket_port if definition else None
        answer = self.reported.get(host)
        if answer and (not adapter or adapter == answer.adapter):
            number = answer.socket_port or DEVICE_DEFINITIONS[answer.adapter].socket_port
        self.port.setEnabled(kind == "ip" and suffix is None)
        self.port.blockSignals(True)
        self.port.setValue(suffix or number or 0)
        self.port.blockSignals(False)
        self.update_preview()

    def update_preview(self):
        try:
            kind, host, suffix = self.input()
        except ValueError as exc:
            self.preview.setText(str(exc))
            return
        answer = self.reported.get(host)
        identity = f"{answer.identity.model} · {answer.identity.serial} · " if answer else ""
        port = suffix or self.port.value()
        protocol = (
            f"串口 {host}"
            if kind == "serial"
            else f"SCPI Socket {host}:{port}"
            if port
            else f"VISA INSTR {host} / inst0"
        )
        self.preview.setText(identity + protocol)

    def address_changed(self):
        self.defaults()
        self.debounce.start(300)
        self.changed()

    def lookup(self):
        if self.waiting():
            _, host, _ = self.input()
            if host not in self.pending:
                self.pending[host] = self.window.bridge.worker.submit(identify_endpoint, host)
                self.poll.start()

    def collect(self):
        for host, future in tuple(self.pending.items()):
            if future.done():
                del self.pending[host]
                try:
                    self.reported[host] = future.result()
                except (OSError, ValueError):
                    self.reported[host] = None
                if host == self.endpoint.text().strip().partition(":")[0]:
                    if self.custom_port:
                        self.update_preview()
                    else:
                        self.defaults()
        if not self.pending:
            self.poll.stop()
        self.changed()


def connection_error(code, detail):
    message = detail.casefold()
    if code in {"resource_busy", "device_busy", "task_busy"} or any(
        s in message for s in ("vi_error_rsrc_locked", "10048", "占用", "resource busy")
    ):
        return "设备已占用，请核对当前会话"
    if code in {"identity_conflict", "model_mismatch", "identity_mismatch", "unsupported_model"} or any(
        s in message for s in ("does not match", "mismatch", "不匹配", "型号不符", "不一致")
    ):
        return "型号或序列号不匹配，请核对所选型号与仪器铭牌"
    if any(
        s in message for s in ("10051", "10065", "host unreachable", "network is unreachable", "no route")
    ):
        return "地址不可达，请核对 IP 和实验网卡"
    if code == "timeout" or any(
        s in message for s in ("10060", "10061", "timed out", "timeout", "vi_error_tmo", "refused")
    ):
        return "端口未响应，请核对协议、端口和仪器的远程接口"
    if "vi_error_rsrc_nfound" in message:
        return "未找到 VISA 资源，请核对地址、协议和端口"
    return "连接失败"


def scan_result_text(runtime, report):
    states = {s["stableDeviceId"]: s for s in runtime.sessions.snapshots()}
    records = {r["id"]: r for r in runtime.inventory.all()}
    retry = runtime.connections.snapshots()
    lines = [
        f"{name} · {row.get('error', '')}"
        for name, row in report["providers"].items()
        if row["state"] == "error"
    ]
    for item in report.get("items", ()):
        key = item.get("device_id")
        record = records.get(key, {})
        state = states.get(key, {})
        ident = state.get("identity") or record.get("identity") or {}
        error = item.get("error") or state.get("lastError") or retry.get(key, {}).get("error")
        if error:
            code = item.get("errorCode") or state.get("connectionErrorCode") or retry.get(key, {}).get("code")
            result = f"{connection_error(code, error)} · {error}"
        elif state.get("connectionState") in {"connected", "busy"}:
            result = "已连接"
        elif key in runtime.connections.suppressed_devices():
            result = "已手动断开"
        elif review := identity_review_reason(record):
            result = f"待核对：{review}"
        elif item.get("reason") in {"identified", "sdk_identity_requires_explicit_connection"}:
            result = retry.get(key, {}).get("label") or ("正在连接" if retry.get(key) else "已识别")
        elif item.get("reason") == "deferred_possible_alias_of_active_device":
            result = "待核对：该入口可能属于已占用的设备"
        else:
            result = "待核对：尚未取得独立设备身份"
        lines.append(
            " · ".join(filter(None, (item["resource"], ident.get("model"), ident.get("serial"), result)))
        )
    return "\n".join(lines) or "所选地址没有返回设备信息"
