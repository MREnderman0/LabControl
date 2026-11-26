"""Chinese operator notices; scan reports and diagnostic details retain the original errors."""

from __future__ import annotations

import re
from dataclasses import dataclass
from html import escape

from PySide6.QtCore import Qt
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QLabel


@dataclass(frozen=True)
class Notice:
    text: str
    component: str = ""


VISA_MISSING = Notice("缺少 VISA 驱动，部分仪器无法扫描", "rs-visa")
PROVIDERS = {
    "visa": "VISA 接口",
    "lan": "局域网",
    "serial": "串口",
    "sdk": "厂商驱动",
    "static_addresses": "地址列表",
    "remembered_lan": "已登记网络设备",
    "dhcp_leases": "DHCP 租约",
    "configured": "已配置设备",
    "usb_pnp": "USB 设备",
}
ERRORS = {
    "timeout": "设备响应超时，请检查连接后重试",
    "disconnected": "设备连接已断开，请重新连接",
    "connection_failed": "设备连接失败，请检查设备和接口",
    "resource_busy": "设备或接口正在使用，请稍后重试",
    "permission_denied": "访问被拒绝，请检查权限和接口占用",
    "driver_missing": "缺少设备驱动，请在环境与修复中检查",
    "sdk_load_failed": "厂商驱动加载失败，请检查驱动版本和位数",
    "identity_conflict": "设备身份与登记信息不一致，请核对型号和序列号",
    "invalid_parameter": "参数无效，请检查输入值和范围",
    "unsupported": "当前设备或驱动不支持此操作",
    "cleanup_failed": "释放设备资源失败，请检查连接状态",
    "task_busy": "设备正忙，请等待当前任务完成",
    "scan_busy": "正在扫描，请等待当前扫描完成",
    "maintenance_busy": "设备正在使用，请结束采集或连接后再维护",
    "parse_error": "无法解析仪器返回的数据，请检查型号和通信设置",
    "readback_mismatch": "仪器读回值与设置不符，请核对当前参数",
    "identity_required": "请先核对设备身份再继续",
    "recording_failed": "记录写入失败，请检查保存目录和磁盘空间",
    "acquisition_failed": "采集失败，请检查仪器状态和采集设置",
    "configuration_changed": "仪器配置已变化，请核对当前参数",
    "write_uncertain": "写入结果未确认，请先读回仪器设置",
    "restoration_failed": "恢复原设置失败，请核对设备或接口设置",
}
# Some backends and older scan reports only supply free text.
ERROR_TERMS = (
    ("permission_denied", ("permission denied", "access is denied", "access denied", "vi_error_npermission")),
    (
        "resource_busy",
        (
            "resource busy",
            "resource is busy",
            "resource locked",
            "vi_error_rsrc_busy",
            "vi_error_rsrc_locked",
        ),
    ),
    ("timeout", ("timeout", "timed out", "vi_error_tmo")),
    ("disconnected", ("disconnected", "connection lost", "connection reset", "vi_error_conn_lost")),
    ("connection_failed", ("offline", "connection refused", "could not open port", "vi_error_rsrc_nfound")),
)


def explain(detail: str, code: str = "", provider: str = "") -> Notice:
    lower = detail.casefold()
    if "could not locate a visa implementation" in lower:
        return VISA_MISSING
    if "visa runtime unavailable" in lower:
        return Notice("VISA 驱动无法加载，请检查安装和位数", "rs-visa")
    if "no r&s lan discovery extension" in lower:
        return Notice("当前 VISA 驱动不支持局域网扫描", "rs-visa")
    if code == "driver_missing" and provider == "visa":
        return VISA_MISSING
    if detail.startswith(("原始数据已保存", "读数已保存")):
        return Notice(detail)
    if detail.startswith("关闭期间发生错误"):
        return Notice("关闭期间发生错误，请查看详细信息")
    if detail.startswith("记录未完整完成"):
        return Notice("记录未完整完成，请查看详细信息和记录目录")
    if code in ERRORS:
        return Notice(ERRORS[code])
    for key, terms in ERROR_TERMS:
        if any(term in lower for term in terms):
            return Notice(ERRORS[key])
    if re.search(r"[\u3400-\u9fff]", detail):
        # A localized prefix can wrap a raw exception, e.g. “创建采集批次失败：...”.
        prefix, separator, tail = detail.partition("：")
        if (
            separator
            and not re.search(r"[\u3400-\u9fff]", tail)
            and any(word in prefix for word in ("失败", "异常", "错误"))
        ):
            return Notice(prefix + "，请查看详细信息")
        return Notice(detail)
    if provider:
        return Notice(PROVIDERS.get(provider.split(":")[0], "设备") + "扫描失败，请查看详细信息")
    return Notice("操作失败，请查看详细信息")


def scan_notices(report: dict) -> tuple[list[Notice], str]:
    problems, details = [], []
    for name, data in sorted(report.get("providers", {}).items()):
        if data["state"] == "error":
            raw = str(data.get("error") or "")
            problems.append(explain(raw, data.get("errorCode", ""), name))
            details.append(f"{name} · {raw}")
    for item in report.get("items", ()):
        if raw := item.get("error"):
            problems.append(explain(raw, item.get("errorCode", ""), "设备"))
            details.append(f"{item.get('resource', '')} · {raw}")
    unique = list(dict.fromkeys(problems))
    unique.sort(key=lambda notice: not bool(notice.component))
    return unique, "\n".join(details)


class StatusMessage(QLabel):
    """Only locally constructed repair links are interactive; all external text is escaped."""

    def setText(self, text: str) -> None:  # noqa: N802
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setToolTip("")
        self.setProperty("kind", "")
        super().setText(text)
        self.style().unpolish(self)
        self.style().polish(self)

    def present(self, notices: list[Notice], detail: str) -> None:
        first = notices[0]
        text = first.text + (f"（另有 {len(notices) - 1} 项提示）" if len(notices) > 1 else "")
        self.setText(text)
        self.setProperty("kind", "err")
        self.style().unpolish(self)
        self.style().polish(self)
        if first.component:
            self.setTextFormat(Qt.TextFormat.RichText)
            self.setTextInteractionFlags(
                Qt.TextInteractionFlag.LinksAccessibleByMouse
                | Qt.TextInteractionFlag.LinksAccessibleByKeyboard
            )
            self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            color = self.palette().color(QPalette.ColorRole.WindowText).name()
            super().setText(f'<a style="color:{color}" href="{escape(first.component)}">{escape(text)}</a>')
        lines = [n.text for n in notices]
        if first.component:
            lines.append("点击进入环境与修复，准备对应组件")
        if detail and detail != first.text:
            lines.extend(("技术详情", detail))
        self.setToolTip("<p>" + "<br>".join(escape(line).replace("\n", "<br>") for line in lines) + "</p>")
