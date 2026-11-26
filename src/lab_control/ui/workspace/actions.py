from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QInputDialog,
    QLabel,
    QTextBrowser,
    QVBoxLayout,
)

from lab_control.application.bulk_connection import device_label
from lab_control.application.catalog import instrument_categories, instrument_category
from lab_control.application.config import config_from_record
from lab_control.application.controls import CATEGORY_LABELS
from lab_control.domain.instruments import jsonable
from lab_control.instruments.registry import DEVICE_DEFINITIONS, describe, requires_manual_connection
from lab_control.ui.widgets.slot_picker import choose_slot
from lab_control.ui.widgets.workspace_combo import choose_item
from lab_control.ui.workspace.status import explain, scan_notices

if TYPE_CHECKING:
    from lab_control.ui.real_window import RealWindow


class WorkspaceActions:
    """User-requested device actions and asynchronous result presentation."""

    def __init__(self, window: RealWindow) -> None:
        self.window = window

    def prepare_first_run(self) -> None:
        window = self.window
        from lab_control.ui.pages.component_preparation import show_preparation

        show_preparation(window.runtime, window, first_run=True)
        window.scan()
        if window.runtime.hardware:
            window.runtime.serial_autoconnect.start()

    def scan(self) -> None:
        window = self.window
        try:
            window.bridge.watch(window.runtime.discovery.scan(), "scan")
            window.refresh()
        except Exception as exc:
            window._failed(exc)

    def action(self, key: str) -> None:
        window = self.window
        device_id = window.selected_id
        try:
            if key == "connect":
                config = config_from_record(window.records[device_id])
                if requires_manual_connection(config.adapter) and window.runtime.hardware:
                    window.show_manual_serial(config.adapter)
                    return
                # A USB-to-serial bridge identifies the connection, not the
                # instrument serial. Reuse its saved adapter as bulk connect does.
                definition = DEVICE_DEFINITIONS.get(config.adapter)
                if (
                    config.usb_instance_id
                    and config.transport != "serial"
                    and not config.expected_serial
                    and not (definition and definition.manual_usb_id)
                ):
                    serial, accepted = QInputDialog.getText(
                        window, "核对设备序列号", "请输入设备铭牌序列号；连接后将与设备实际读回再次核对"
                    )
                    if not accepted or not serial.strip():
                        return
                    from dataclasses import replace

                    window.runtime.add(replace(config, expected_serial=serial.strip()))
                window.bridge.watch(window.runtime.connections.request(device_id), "connect")
            elif key in {"disconnect", "reconnect"}:
                if (
                    key == "reconnect"
                    and window.runtime.hardware
                    and requires_manual_connection(window.records[device_id]["adapterId"])
                ):
                    window._failed("请先断开原会话，再手动选择泵浦串口。")
                    return

                connections = window.runtime.connections
                future = (
                    connections.disconnect(device_id)
                    if key == "disconnect"
                    else connections.request(device_id, reconnect=True)
                )
                window.bridge.watch(future, key)
            elif key in {"identify", "status"}:
                window.bridge.watch(window.runtime.sessions.submit(device_id, key), key)
            elif key == "capabilities":
                snapshot = window.runtime.sessions.capability_snapshot(device_id)
                window._completed(
                    "capabilities", snapshot or describe(config_from_record(window.records[device_id]))
                )
            elif key == "stop":
                window.runtime.sessions.stop_acquisition(device_id)
                window.controller.watch(window.runtime.monitoring.stop(device_id))
            elif key in {"manage", "monitor"}:
                category = instrument_category(window.records[device_id])
                if window.active_category in instrument_categories(window.records[device_id]):
                    category = window.active_category
                if category in CATEGORY_LABELS:
                    window.navigate("设备管理" if key == "manage" else "实时监测", category, device_id)
            elif key == "diagnostic":
                window._completed(
                    "diagnostics",
                    {"device": window.records[device_id], "session": window.states.get(device_id, {})},
                )
            elif key == "resource":
                value, accepted = choose_item(
                    window,
                    "主连接资源",
                    "选择已发现的资源",
                    window.records[device_id]["resources"],
                )
                if accepted:
                    window.runtime.select_resource(device_id, value)
            elif key == "configure":
                window.configure_adapter(device_id)
            elif key == "rename":
                value, accepted = QInputDialog.getText(window, "设备显示名", "名称")
                if accepted:
                    window.runtime.inventory.edit(device_id, display_name=value)
            elif key == "channel_names":
                from lab_control.ui.widgets.channel_names import ChannelNamesDialog

                ChannelNamesDialog(window.runtime, device_id, window.controller.mode, window).exec()
            elif key == "slot":
                window.choose_slot(device_id)
            elif key == "rename_slot":
                from lab_control.ui.widgets.slot_picker import edit_slot_name

                mode = window.controller.mode
                slot = window.records[device_id].get("slots", {}).get(mode)
                if slot:
                    edit_slot_name(window, window.runtime, slot, mode)
        except Exception as exc:
            window._failed(exc)
        window.refresh()

    def choose_slot(self, device_id: str) -> None:
        window = self.window
        mode = window.controller.mode
        records = window.runtime.inventory.all()
        record = next(r for r in records if r["id"] == device_id)
        category = instrument_category(record)
        slot, accepted = choose_slot(window, window.runtime, device_id, category, mode)
        if not accepted or (
            slot == record.get("slots", {}).get(mode, "") and not record.get("slotMigrationConflict")
        ):
            return
        records = window.runtime.inventory.all()
        owners = {r.get("slots", {}).get(mode): r for r in records if r.get("slots", {}).get(mode)}
        owner = owners.get(slot)
        replace_id = None
        if owner and owner["id"] != device_id:
            dialog = QDialog(window)
            dialog.setWindowTitle("替换槽位绑定")
            dialog.setMinimumWidth(420)
            layout = QVBoxLayout(dialog)
            label = window.runtime.repository.function_slots.label(slot, mode)
            prompt = QLabel(f"已有【{device_label(owner)}】绑定【{label}】，是否替换？")
            prompt.setTextFormat(Qt.TextFormat.PlainText)
            prompt.setWordWrap(True)
            layout.addWidget(prompt)
            note = QLabel("替换后，原设备保持连接，槽位变为未绑定。")
            note.setWordWrap(True)
            layout.addWidget(note)
            buttons = QDialogButtonBox(
                QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
            )
            confirm = buttons.button(QDialogButtonBox.StandardButton.Ok)
            confirm.setText("替换")
            confirm.setAutoDefault(False)
            cancel = buttons.button(QDialogButtonBox.StandardButton.Cancel)
            cancel.setText("取消")
            cancel.setDefault(True)
            cancel.setFocus()
            buttons.accepted.connect(dialog.accept)
            buttons.rejected.connect(dialog.reject)
            layout.addWidget(buttons)
            accepted = dialog.exec() == QDialog.DialogCode.Accepted
            dialog.deleteLater()
            if not accepted:
                return
            replace_id = owner["id"]
        window.runtime.bind_slot(device_id, slot, mode, replace_device_id=replace_id)

    def connect_all(self) -> None:
        window = self.window
        try:
            window.bridge.watch(window.runtime.connect_all_discovered(), "connect_all")
        except Exception as exc:
            window._failed(exc)
        window.refresh()

    def show_connection_results(self) -> None:
        window = self.window
        progress = window.runtime.connection_progress()
        dialog = QDialog(window)
        dialog.setWindowTitle("一键连接结果")
        dialog.resize(680, 440)
        layout = QVBoxLayout(dialog)
        summary = QLabel(window.connection_result_text(progress))
        summary.setWordWrap(True)
        layout.addWidget(summary)
        text = QTextBrowser()
        labels = {
            "connected": "成功",
            "already_connected": "已连接",
            "failed": "失败",
            "skipped": "跳过",
            "waiting": "等待恢复",
        }
        text.setPlainText(
            "\n\n".join(
                f"{item['label']}\n{labels[item['status']]}：{item['reason']}" for item in progress["items"]
            )
        )
        layout.addWidget(text)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("关闭")
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()
        dialog.deleteLater()

    def _completed(self, label: str, result: Any) -> None:
        window = self.window
        labels = {
            "connect": "设备已连接",
            "reconnect": "设备已重新连接",
            "disconnect": "设备已断开",
            "identify": "设备识别完成",
            "status": "设备状态已读取",
            "capabilities": "设备能力已读取",
            "diagnostics": "设备诊断完成",
        }
        window.message.setText(labels.get(label, "操作完成"))
        if label == "connect_all":
            window.message.setText(window.connection_result_text(result))
        elif label in {"capabilities", "diagnostics"}:
            dialog = QDialog(window)
            dialog.setWindowTitle("能力与诊断")
            dialog.resize(760, 600)
            layout = QVBoxLayout(dialog)
            text = QTextBrowser()
            text.setPlainText(json.dumps(jsonable(result), ensure_ascii=False, indent=2))
            layout.addWidget(text)
            dialog.exec()
        elif label in {"scan", "address_scan"}:
            notices, detail = scan_notices(result)
            if notices:
                window.message.present(notices, detail)
            else:
                window.message.setText(
                    "扫描已取消"
                    if result.get("state") == "cancelled"
                    else "地址列表扫描完成"
                    if label == "address_scan"
                    else "扫描完成"
                )
            if label == "address_scan":
                if window._static_address_status is not None:
                    window._static_address_status.setTextFormat(Qt.TextFormat.PlainText)
                    window._static_address_status.setText(
                        "；".join(n.text for n in notices) or window.message.text()
                    )
                    window._static_address_status.setToolTip(window.message.toolTip())
                if window._refresh_static_address_dialog is not None:
                    window._refresh_static_address_dialog()
        window.refresh()

    def _failed(self, message: str | Exception) -> None:
        window = self.window
        detail = str(message)
        notice = explain(detail, getattr(message, "code", ""))
        if (
            notice.text == detail
            and isinstance(message, str)
            and not any(flag in detail for flag in ("失败", "异常", "错误"))
        ):
            window.message.setText(detail)
        else:
            window.message.present([notice], detail)
        if window._static_address_status is not None:
            window._static_address_status.setTextFormat(Qt.TextFormat.PlainText)
            window._static_address_status.setText(notice.text)
            window._static_address_status.setToolTip(window.message.toolTip())
        if window.runtime.closed:
            window.closing = True
            window.bridge.worker.shutdown(wait=False)
            window.close()

    def prepare_component(self, component: str) -> None:
        window = self.window
        window.navigate("系统设置")
        window.settings_page.ensureWidgetVisible(window.environment_panel, 0, 0)
        from lab_control.ui.pages.component_preparation import show_preparation

        show_preparation(window.runtime, window.environment_panel, component=component)

    def diagnose(self) -> None:
        window = self.window
        window.navigate("系统设置")
        window.environment_panel.start()
