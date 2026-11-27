from __future__ import annotations

import re
from dataclasses import replace
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from lab_control.application.config import config_from_record
from lab_control.domain.instruments import DeviceError
from lab_control.instruments.registry import (
    DEVICE_DEFINITIONS,
    PROFILES,
    SDK_ADAPTERS,
    requires_manual_connection,
)
from lab_control.ui.widgets.workspace_combo import WorkspaceComboBox, choose_item

if TYPE_CHECKING:
    from lab_control.ui.real_window import RealWindow


class ConnectionDialogs:
    """Explicit connection configuration dialogs."""

    def __init__(self, window: RealWindow) -> None:
        self.window = window

    def connect_manual_serial(self, adapter: str) -> None:
        from lab_control.discovery.providers import SerialDiscoveryProvider

        window = self.window
        definition = DEVICE_DEFINITIONS[adapter]
        title = definition.documentation.title
        try:
            ports = sorted(
                {
                    candidate.config.resource
                    for candidate in SerialDiscoveryProvider().enumerate()
                    if re.fullmatch(r"COM[1-9][0-9]*", candidate.config.resource, re.I)
                    and definition.matches_manual_port(candidate.descriptor)
                },
                key=lambda port: int(port[3:]),
            )
            if not ports:
                window._failed(f"{title} 未发现匹配的 COM 口，请检查串口接线与驱动。")
                return
            port, accepted = choose_item(window, title, "请核对接线并选择设备所接的 COM 口。", ports)
            if not accepted:
                return
            device_id, future = window.runtime.connect_manual_serial(adapter, port)
            window.message.setText(f"正在连接 {title} · {port}…")

            def connected(_identity) -> None:
                window.refresh()
                window.navigate("设备管理", definition.category, device_id)
                window.message.setText(f"{title} · {port} · {definition.manual_connection_note}")

            window.controller.watch(future, connected)
        except (DeviceError, OSError, ValueError) as exc:
            window._failed(str(exc))

    def show_static_addresses(self) -> None:
        """Open the compact model-and-endpoint dialog."""
        window = self.window
        if window._static_address_dialog is not None:
            window._static_address_dialog.raise_()
            window._static_address_dialog.activateWindow()
            return

        dialog = QDialog(window)
        dialog.setObjectName("staticAddressDialog")
        dialog.setWindowTitle("添加设备与地址列表")
        dialog.resize(760, 560)
        window._static_address_dialog = dialog
        root = QVBoxLayout(dialog)

        title = QLabel("添加设备")
        title.setObjectName("panelTitle")
        root.addWidget(title)
        description = QLabel("输入 IPv4、IP:端口 或 COM 口。确认连接预览后即可保存并识别。")
        description.setObjectName("pageDescription")
        description.setWordWrap(True)
        root.addWidget(description)

        form = QFormLayout()
        model = WorkspaceComboBox()
        model.addItem("自动识别（只读探测）", "")
        for adapter_id, profile in sorted(PROFILES.items()):
            manufacturer = profile.manufacturer[0] if profile.manufacturer else "未知厂家"
            models = "/".join(profile.models[:2]) or adapter_id
            name = f"{manufacturer} {models}"
            if len(profile.models) > 2:
                name = DEVICE_DEFINITIONS[adapter_id].documentation.title
            model.addItem(f"{name} · {adapter_id}", adapter_id)
        for adapter_id, definition in sorted(DEVICE_DEFINITIONS.items()):
            if adapter_id not in PROFILES and definition.socket_port is not None:
                model.addItem(f"{definition.documentation.title} · {adapter_id}", adapter_id)
        endpoint = QLineEdit()
        endpoint.setObjectName("staticAddressInput")
        endpoint.setPlaceholderText("例如 192.168.5.241 或 COM3")
        from lab_control.ui.workspace.endpoint_fields import EndpointFields

        fields = EndpointFields(dialog, form, window, model, endpoint)
        port = fields.port
        root.addLayout(form)

        advanced_toggle = QCheckBox("显示高级连接选项")
        root.addWidget(advanced_toggle)
        advanced = QWidget()
        advanced_form = QFormLayout(advanced)
        baud = QSpinBox()
        baud.setRange(300, 3_000_000)
        baud.setValue(9600)
        timeout = QDoubleSpinBox()
        timeout.setRange(0.1, 60)
        timeout.setValue(3)
        label = QLineEdit()
        label.setPlaceholderText("可选，例如实验室光谱仪")
        source_address = QLineEdit()
        source_address.setObjectName("staticSourceAddressInput")
        source_address.setPlaceholderText("可选，填写实验网卡 IPv4；6362D 留空自动选择物理网卡")
        advanced_form.addRow("串口波特率（COM）", baud)
        advanced_form.addRow("通信超时（秒）", timeout)
        advanced_form.addRow("备注（可选）", label)
        advanced_form.addRow("本地网卡地址（可选）", source_address)
        advanced.setVisible(False)
        advanced_toggle.toggled.connect(advanced.setVisible)
        root.addWidget(advanced)

        address_count = QLabel()
        address_count.setObjectName("overviewNote")
        root.addWidget(address_count)
        address_list = QListWidget()
        address_list.setObjectName("staticAddressList")
        address_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        root.addWidget(address_list, 1)

        actions = QHBoxLayout()
        save_scan = QPushButton("保存并扫描")
        save_scan.setObjectName("primaryButton")
        editing = ""
        edit = QPushButton("修改选中")
        remove = QPushButton("删除选中")
        scan_selected = QPushButton("重试连接")
        scan_all = QPushButton("扫描全部")
        actions.addWidget(save_scan)
        actions.addWidget(edit)
        actions.addWidget(remove)
        actions.addWidget(scan_selected)
        actions.addWidget(scan_all)
        actions.addStretch()
        root.addLayout(actions)
        status = QLabel()
        status.setObjectName("overviewNote")
        status.setWordWrap(True)
        root.addWidget(status)

        def update_results():
            report = status.property("scanReport")
            if report:
                from lab_control.ui.workspace.endpoint_fields import scan_result_text

                status.setText(scan_result_text(window.runtime, report))

        tick = QTimer(dialog)
        tick.timeout.connect(update_results)
        tick.start(500)

        def refresh_addresses() -> None:
            update_results()
            entries = window.runtime.static_addresses.entries()
            selected = {str(item.data(Qt.ItemDataRole.UserRole)) for item in address_list.selectedItems()}
            address_list.blockSignals(True)
            address_list.clear()
            for entry in entries:
                kind = "COM" if entry.kind == "serial" else "IP"
                model_name = entry.adapter or "自动识别"
                suffix = f" · {entry.label}" if entry.label else ""
                source_suffix = f" · 源 {entry.source_address}" if entry.source_address else ""
                item = QListWidgetItem(f"{kind} {entry.resource} · {model_name}{suffix}{source_suffix}")
                item.setData(Qt.ItemDataRole.UserRole, entry.id)
                address_list.addItem(item)
                item.setSelected(entry.id in selected)
            address_list.blockSignals(False)
            address_count.setText(f"已保存 {len(entries)} 个设备地址")
            update_address_actions()

        def update_address_actions() -> None:
            has_selection = bool(address_list.selectedItems())
            has_items = address_list.count() > 0
            busy = window.runtime.discovery.is_scanning
            edit.setEnabled(len(address_list.selectedItems()) == 1)
            remove.setEnabled(has_selection)
            scan_selected.setEnabled(has_selection and not busy)
            scan_all.setEnabled(has_items and not busy)
            save_scan.setEnabled(not busy and not fields.waiting())

        fields.changed = update_address_actions

        window._refresh_static_address_dialog = refresh_addresses

        def selected_ids() -> tuple[str, ...]:
            return tuple(str(item.data(Qt.ItemDataRole.UserRole)) for item in address_list.selectedItems())

        def scan_addresses(ids: tuple[str, ...], *, retry=False) -> None:
            status.setProperty("scanReport", None)
            if not ids:
                status.setText("请先选择至少一个设备地址。")
                return
            try:
                window.bridge.watch(window.runtime.scan_static_addresses(ids, retry=retry), "address_scan")
                status.setText("正在扫描所选设备地址…")
                refresh_addresses()
            except (DeviceError, ValueError) as exc:
                status.setText(str(exc))

        def save_endpoint() -> None:
            nonlocal editing
            status.setProperty("scanReport", None)
            try:
                kind, value, suffix = fields.input()
                chosen = str(model.currentData() or "")
                answer = fields.reported.get(value)
                if answer and chosen and answer.adapter != chosen:
                    raise ValueError(
                        f"型号不匹配：仪器回报 {answer.identity.model} · {answer.identity.serial}"
                    )
                from lab_control.application.static_addresses import StaticAddress

                resource = StaticAddress("", value, suffix or port.value(), chosen, kind=kind).resource
                existing = next(
                    (
                        item
                        for item in window.runtime.static_addresses.entries()
                        if item.id != editing
                        and (
                            item.resource == resource
                            or (
                                not editing
                                and suffix is None
                                and not fields.custom_port
                                and item.address == value
                            )
                        )
                    ),
                    None,
                )
                if existing:
                    for i in range(address_list.count()):
                        item = address_list.item(i)
                        if item.data(Qt.ItemDataRole.UserRole) == existing.id:
                            address_list.setCurrentItem(item)
                            address_list.scrollToItem(item)
                            break
                    status.setText("已定位到已有地址，可点击「重试连接」或「修改选中」更正。")
                    return
                entry = window.runtime.static_addresses.add(
                    value,
                    kind=kind,
                    port=suffix or port.value(),
                    adapter=chosen or (answer.adapter if answer else ""),
                    edit_id=editing,
                    timeout=timeout.value(),
                    label=label.text(),
                    baud_rate=baud.value(),
                    source_address=source_address.text(),
                )
                editing = ""
                endpoint.clear()
                label.clear()
                source_address.clear()
                refresh_addresses()
                if window.runtime.hardware:
                    scan_addresses((entry.id,))
                else:
                    status.setText("已保存；模拟模式不会访问真实设备。")
            except (DeviceError, ValueError) as exc:
                status.setText(str(exc))

        def edit_selected():
            nonlocal editing
            entries = window.runtime.static_addresses.by_ids(selected_ids())
            if len(entries) != 1:
                return
            entry = entries[0]
            status.setProperty("scanReport", None)
            editing = entry.id
            fields.search.clear()
            model.setCurrentIndex(max(0, model.findData(entry.adapter)))
            endpoint.setText(entry.address)
            port.setValue(entry.port)
            baud.setValue(entry.baud_rate)
            timeout.setValue(entry.timeout)
            label.setText(entry.label)
            source_address.setText(entry.source_address)
            status.setText("正在修改选中地址，保存后将重新识别。")

        edit.clicked.connect(edit_selected)
        save_scan.clicked.connect(save_endpoint)

        def remove_selected() -> None:
            window.runtime.static_addresses.remove(set(selected_ids()))
            refresh_addresses()

        remove.clicked.connect(remove_selected)
        scan_selected.clicked.connect(lambda: scan_addresses(selected_ids(), retry=True))
        scan_all.clicked.connect(
            lambda: scan_addresses(tuple(entry.id for entry in window.runtime.static_addresses.entries()))
        )
        address_list.itemSelectionChanged.connect(update_address_actions)
        refresh_addresses()

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("关闭")
        buttons.rejected.connect(dialog.reject)
        root.addWidget(buttons)
        window._static_address_status = status

        def clear_dialog() -> None:
            if window._static_address_dialog is dialog:
                window._static_address_dialog = None
                window._static_address_status = None
                window._refresh_static_address_dialog = None

        dialog.finished.connect(lambda _result: clear_dialog())
        dialog.exec()
        dialog.deleteLater()

    def configure_adapter(self, device_id: str) -> None:
        """Let the user bind a known protocol to one enumerated resource."""
        window = self.window
        record = window.records[device_id]
        current = config_from_record(record)
        if requires_manual_connection(current.adapter) and window.runtime.hardware:
            self.connect_manual_serial(current.adapter)
            return
        dialog = QDialog(window)
        dialog.setWindowTitle("为已发现资源指定适配器")
        form = QFormLayout(dialog)
        resource = QLineEdit(current.resource)
        serial = QLineEdit(current.expected_serial)
        baud, timeout = QSpinBox(), QDoubleSpinBox()
        baud.setRange(300, 3000000)
        baud.setValue(current.baud_rate)
        timeout.setRange(0.1, 60)
        timeout.setValue(current.timeout)
        adapter, transport = WorkspaceComboBox(), WorkspaceComboBox()
        adapter.addItems([key for key in (*PROFILES, *SDK_ADAPTERS) if not requires_manual_connection(key)])
        if current.adapter:
            adapter.setCurrentText(current.adapter)
        transport.addItems(["visa", "serial", "tcp", "vicp", "tlpmx", "pm100d", "pico", "tserve", "kplusk"])
        transport.setCurrentText(current.transport)
        resource.setReadOnly(False)
        form.addRow("资源地址", resource)
        form.addRow("准确型号适配器", adapter)
        form.addRow("传输方式", transport)
        form.addRow("预期序列号（可选）", serial)
        form.addRow("串口波特率", baud)
        form.addRow("通信超时（秒）", timeout)
        note = QLabel("COM 号只代表本次插入地址；确认适配器后，匹配的 USB 转串口设备会随扫描自动核验。")
        note.setWordWrap(True)
        form.addRow(note)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if dialog.exec():
            try:
                # The form edits only these fields. Preserve safety limits and
                # model/transport settings that are not exposed by this dialog.
                config = replace(
                    current,
                    resource=resource.text().strip(),
                    adapter=adapter.currentText(),
                    transport=transport.currentText(),
                    expected_serial=serial.text().strip(),
                    baud_rate=baud.value(),
                    timeout=timeout.value(),
                    identity_confirmed=False,
                    source_address=current.source_address if transport.currentText() == "tcp" else "",
                )
                window.runtime.configure_device(device_id, config)
                window.refresh()
            except (DeviceError, ValueError) as exc:
                window._failed(str(exc))
