"""Explicit component selection and download, shared by first run and settings."""

from __future__ import annotations

import html
import os
import threading
from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextBrowser,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from lab_control.application.config import ConnectionConfig
from lab_control.deployment.catalog import default_components, load_manifest, required_components, user_cache
from lab_control.deployment.environment import inspect_environment
from lab_control.deployment.materials import (
    check_file,
    component_closure,
    prepare_components,
    safe_relative,
    sources,
    verify_component,
)
from lab_control.ui.widgets.workspace_combo import WorkspaceComboBox


def configured_devices(runtime):
    configs = list(runtime.configurations)
    for record in runtime.inventory.all():
        try:
            config = ConnectionConfig(**record["config"])
            if config not in configs:
                configs.append(config)
        except (KeyError, TypeError, ValueError):
            continue
    return tuple(configs)


class PreparationSignals(QObject):
    progress = Signal(object)
    finished = Signal(object)
    installer = Signal(object)


class ComponentPreparationPanel(QWidget):
    def __init__(
        self,
        runtime,
        parent=None,
        *,
        preparer=prepare_components,
        inspector=inspect_environment,
        launcher=None,
    ):
        super().__init__(parent)
        self.runtime, self.preparer, self.inspector = runtime, preparer, inspector
        self.launcher = launcher or (lambda path: os.startfile(str(path)))
        manifest = load_manifest()
        self.catalog = {c["id"]: c for c in manifest["components"]}
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="component-prepare")
        self.cancel_event = threading.Event()
        self.future = None
        self.disposed = False
        self.installing = False
        self.preparing_runtime = False
        self.results: dict = {}
        self.inspections: dict = {}
        self.signals = PreparationSignals(self)
        self.signals.progress.connect(self.update_item)
        self.signals.finished.connect(self.completed)
        self.signals.installer.connect(self.open_verified_installer)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.status = QLabel("公共驱动和工具已勾选，可按接口调整。勾选所需型号，再点击“准备所选组件”。")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.device_choice = WorkspaceComboBox()
        self.device_choice.addItem("选择设备或接口（可多次添加，再按实际接口调整组件）", [])
        from lab_control.instruments.devices.catalog import load_definitions

        for definition in load_definitions().values():
            self.device_choice.addItem(definition.documentation.title, list(definition.deployment_components))
        for label, keys in (
            ("PicoScope 6404E / PicoSDK", ["pico-sdk"]),
            ("Thorlabs PM100D / TLPMX", ["thorlabs-opm"]),
            ("通用 VISA 设备（USB / LAN）", ["rs-visa"]),
            ("GPIB 接口（另需核对控制器型号）", ["rs-visa", "gpib-interface"]),
            ("串口接口（另需核对转换器型号）", ["serial-interface"]),
        ):
            self.device_choice.addItem(label, keys)
        self.device_choice.currentIndexChanged.connect(self.choose_device)
        layout.addWidget(self.device_choice)
        self.table = QTreeWidget()
        self.table.setHeaderLabels(["设备组件", "版本", "准备状态"])
        self.table.setRootIsDecorated(True)
        self.table.setMinimumHeight(140)
        self.rows = {}
        configs = configured_devices(runtime)
        recommended = required_components(configs)
        selected = set(default_components(manifest))
        parents = {}
        for scope, title in (("common", "公共运行库和接口工具"), ("model", "型号专用组件")):
            group = QTreeWidgetItem(self.table, [title])
            group.setFlags(group.flags() | Qt.ItemFlag.ItemIsAutoTristate | Qt.ItemFlag.ItemIsUserCheckable)
            parents[scope] = group
        member_groups = {}
        for c in self.catalog.values():
            if c.get("members"):
                group = QTreeWidgetItem(parents[c["scope"]], [c["name"]])
                group.setFlags(
                    group.flags() | Qt.ItemFlag.ItemIsAutoTristate | Qt.ItemFlag.ItemIsUserCheckable
                )
                member_groups.update(dict.fromkeys(c["members"], group))
        for key, c in self.catalog.items():
            if c.get("members"):
                continue
            row = QTreeWidgetItem(
                member_groups.get(key, parents[c["scope"]]),
                [
                    c["name"],
                    c["version"] or "待核实",
                    "已登记设备推荐" if key in recommended and c["scope"] == "model" else "未准备",
                ],
            )
            row.setData(0, Qt.ItemDataRole.UserRole, key)
            row.setCheckState(0, Qt.CheckState.Checked if key in selected else Qt.CheckState.Unchecked)
            self.rows[key] = row
        self.table.expandAll()
        self.table.setColumnWidth(0, 320)
        self.table.setColumnWidth(1, 190)
        layout.addWidget(self.table)
        self.details = QTextBrowser()
        self.details.setOpenExternalLinks(True)
        self.details.setMaximumHeight(95)
        layout.addWidget(self.details)
        buttons = QHBoxLayout()
        self.prepare_button = QPushButton("准备所选组件")
        self.cancel_button = QPushButton("取消下载")
        self.install_button = QPushButton("打开安装向导")
        self.recheck_button = QPushButton("安装完成，重新检查")
        for button in (self.prepare_button, self.cancel_button, self.install_button, self.recheck_button):
            buttons.addWidget(button)
        layout.addLayout(buttons)
        self.cancel_button.setEnabled(False)
        self.install_button.setEnabled(False)
        self.prepare_button.clicked.connect(self.prepare)
        self.cancel_button.clicked.connect(self.cancel_event.set)
        self.install_button.clicked.connect(self.install)
        self.recheck_button.clicked.connect(self.recheck)
        self.table.currentItemChanged.connect(self.show_details)
        self.table.itemChanged.connect(self.selection_changed)

    def selected(self):
        return [key for key, row in self.rows.items() if row.checkState(0) == Qt.CheckState.Checked]

    def choose_device(self, index):
        if index <= 0:
            return
        for component in component_closure(self.device_choice.itemData(index) or []):
            self.rows[component["id"]].setCheckState(0, Qt.CheckState.Checked)

    def selection_changed(self, row, column):
        if column != 0:
            return
        key = row.data(0, Qt.ItemDataRole.UserRole)
        if key in self.rows:
            if row.checkState(0) == Qt.CheckState.Checked:
                for dependency in self.catalog[key].get("dependencies", []):
                    self.rows[dependency].setCheckState(0, Qt.CheckState.Checked)
            else:
                for other, component in self.catalog.items():
                    if key in component.get("dependencies", []):
                        self.rows[other].setCheckState(0, Qt.CheckState.Unchecked)
        self.show_details()

    def current_id(self):
        row = self.table.currentItem()
        return row.data(0, Qt.ItemDataRole.UserRole) if row else ""

    def show_details(self, *_args):
        key = self.current_id()
        if key not in self.catalog:
            self.details.clear()
            self.install_button.setEnabled(False)
            return
        c = self.catalog[key]
        labels = {"official-domestic": "官方国内下载", "official-direct": "官方直连下载"}
        links = [
            f'<a href="{html.escape(s["url"], quote=True)}">'
            f"{html.escape(labels.get(s['kind'], s['kind']))}</a>"
            for s in sources(c)
        ]
        if c.get("source_page"):
            links.append(f'<a href="{html.escape(c["source_page"], quote=True)}">官方说明</a>')
        details = [html.escape(c["purpose"]), " · ".join(links)]
        result = self.results.get(key, {})
        if result.get("state") == "prepared" and result.get("path"):
            details.append("已校验材料：" + html.escape(str(result["path"])))
            if not c.get("installer_entry"):
                details.append(html.escape(c["manual_steps"]))
        self.details.setHtml("<br>".join(details))
        self.install_button.setEnabled(
            key in self.selected()
            and bool(c.get("installer_entry"))
            and self.results.get(key, {}).get("state") == "prepared"
            and self.inspections.get(key, {}).get("state") != "ok"
            and not self.installing
            and not self.busy()
        )

    def busy(self):
        return self.future is not None

    def prepare(self):
        if self.busy() or self.installing:
            return
        ids = self.selected()
        if not ids:
            self.status.setText("请选择所需的设备组件；无需下载全部组件。")
            return
        if any(self.catalog[key]["detection"].get("runtime_files") for key in ids):
            try:
                self.runtime.begin_maintenance()
            except Exception as exc:
                self.status.setText(str(exc))
                return
            self.preparing_runtime = True
        configs = configured_devices(self.runtime)
        self.cancel_event = threading.Event()
        self.prepare_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.install_button.setEnabled(False)
        self.status.setText("正在检查已有组件并准备缺少的材料…")

        def work():
            try:
                report = self.inspector(
                    self.runtime.repository.root,
                    configs,
                    selected_components=tuple(ids),
                    cancel=self.cancel_event,
                )
                okay = {i["id"] for i in report.get("items", []) if i["state"] == "ok"}
                closure = [c["id"] for c in component_closure(ids)]
                for item in report.get("items", []):
                    if item["id"] in closure:
                        self.signals.progress.emit(
                            {"id": item["id"], "inspection": item["state"], "reason": item.get("reason", "")}
                        )
                for key in okay.intersection(closure):
                    self.signals.progress.emit({"id": key, "state": "detected"})
                needed = [key for key in closure if key not in okay]
                if not needed and not self.cancel_event.is_set():
                    result = {"passed": True, "state": "complete", "items": []}
                else:
                    result = self.preparer(
                        needed, satisfied=okay, cancel=self.cancel_event, progress=self.signals.progress.emit
                    )
            except Exception as exc:
                result = {"passed": False, "state": "error", "error": str(exc)}
            self.signals.finished.emit(result)

        self.future = self.worker.submit(work)

    def update_item(self, result):
        if self.disposed:
            return
        key = result["id"]
        if "inspection" in result:
            self.inspections[key] = {"state": result["inspection"], "reason": result.get("reason", "")}
            text = {
                "ok": "已检测到兼容组件，设备未验收",
                "unknown": "安装状态待核实",
                "missing": "未检测到组件",
                "incompatible": "组件不兼容",
                "error": "组件检查失败",
                "not_required": "当前未需要",
            }.get(result["inspection"], "安装状态待核实")
            if self.results.get(key, {}).get("state") == "prepared":
                text = "材料已准备 · " + text
            if key in self.rows:
                self.rows[key].setText(2, text)
                self.rows[key].setToolTip(2, result.get("reason", text))
            return
        if result["state"] != "detected" or self.results.get(key, {}).get("state") != "prepared":
            self.results[key] = result
        text = {
            "prepared": "材料已准备，未安装",
            "detected": "已检测到兼容组件，设备未验收",
            "blocked": "准备失败",
            "busy": "其他任务正在准备",
            "cancelled": "已取消",
        }.get(result["state"], "下载中")
        if result["state"] == "downloading":
            text = f"下载 {result['received'] / max(1, result['total']):.0%}"
        if key in self.rows:
            self.rows[key].setText(2, text)
            self.rows[key].setToolTip(2, result.get("error", text))

    def release_preparation(self):
        if self.preparing_runtime:
            self.runtime.end_maintenance()
            self.preparing_runtime = False

    def completed(self, report):
        self.release_preparation()
        if self.disposed:
            return
        self.future = None
        self.prepare_button.setEnabled(not self.installing)
        self.cancel_button.setEnabled(False)
        self.status.setText(
            report.get("message")
            or (
                "所选组件已处理；材料下载不表示已安装或设备已验收。"
                if report.get("passed")
                else "准备未完成，可查看各项结果并重试。" + report.get("error", "")
            )
        )
        self.show_details()

    def install(self):
        key = self.current_id()
        if self.busy() or self.installing or key not in self.selected():
            return
        c = self.catalog[key]
        if not c.get("installer_entry") or self.results.get(key, {}).get("state") != "prepared":
            return
        if self.inspections.get(key, {}).get("state") == "ok":
            self.status.setText("已检测到兼容版本，跳过安装并避免降级。")
            return
        try:
            self.runtime.begin_maintenance()
        except Exception as exc:
            self.status.setText(str(exc))
            return
        self.installing = True
        self.prepare_button.setEnabled(False)
        self.install_button.setEnabled(False)
        self.status.setText("正在重新校验安装程序；设备连接与扫描已暂停…")

        def work():
            try:
                report = self.inspector(
                    self.runtime.repository.root, configured_devices(self.runtime), selected_components=(key,)
                )
                if any(i["id"] == key and i["state"] == "ok" for i in report.get("items", [])):
                    self.signals.installer.emit({"error": "已检测到兼容版本，跳过安装并避免降级。"})
                    return
                verified = verify_component(c, (user_cache(),))
                if verified["state"] != "verified":
                    raise ValueError(verified.get("error", "安装材料未校验"))
                path = user_cache() / safe_relative(c["installer_entry"])
                expected = next(
                    (m for m in c.get("archive_members", []) if m["path"] == c["installer_entry"]), c
                )
                check_file(path, expected)
                self.signals.installer.emit({"id": key, "path": path})
            except Exception as exc:
                self.signals.installer.emit({"error": str(exc)})

        self.future = self.worker.submit(work)

    def open_verified_installer(self, result):
        self.future = None
        if self.disposed:
            self.runtime.end_maintenance()
            return
        try:
            if result.get("error"):
                raise ValueError(result["error"])
            if result["id"] not in self.selected():
                raise ValueError("已取消勾选此组件")
            self.launcher(result["path"])
            self.status.setText(
                "请完成或关闭厂商安装向导，再点击“安装完成，重新检查”。设备连接与扫描保持暂停。"
            )
        except Exception as exc:
            self.runtime.end_maintenance()
            self.installing = False
            self.prepare_button.setEnabled(True)
            self.status.setText("未启动安装向导：" + str(exc))
            self.show_details()

    def recheck(self):
        if self.busy():
            return
        if self.installing:
            self.runtime.end_maintenance()
            self.installing = False
        ids = self.selected()
        configs = configured_devices(self.runtime)
        self.prepare_button.setEnabled(False)

        def work():
            try:
                report = self.inspector(self.runtime.repository.root, configs, selected_components=tuple(ids))
                for item in report.get("items", []):
                    if item["id"] in ids:
                        self.signals.progress.emit(
                            {
                                "id": item["id"],
                                "inspection": item["state"],
                                "reason": item["reason"],
                            }
                        )
                self.signals.finished.emit(
                    {"message": "组件检查完成；材料状态已保留，待核实项不表示安装失败。请查看各项检查结果。"}
                )
            except Exception as exc:
                self.signals.finished.emit({"message": "组件检查失败，已准备的材料保留：" + str(exc)})

        self.future = self.worker.submit(work)

    def shutdown(self):
        self.disposed = True
        self.cancel_event.set()
        self.worker.shutdown(wait=True, cancel_futures=True)
        self.release_preparation()


def show_preparation(runtime, parent, *, first_run=False):
    marker = runtime.repository.root / "maintenance/first-run.json"
    if first_run and (marker.exists() or not runtime.hardware):
        return

    class PreparationDialog(QDialog):
        def done(self, result):
            if panel.installing:
                panel.status.setText("请先完成或关闭安装向导，并点击重新检查，再返回工作台。")
                return
            super().done(result)

    dialog = PreparationDialog(parent)
    dialog.setWindowTitle("首次运行 · 准备设备组件" if first_run else "准备设备组件")
    dialog.resize(850, 600)
    layout = QVBoxLayout(dialog)
    panel = ComponentPreparationPanel(runtime, dialog)
    layout.addWidget(panel)
    skip = QPushButton("稍后准备 / 返回工作台")
    skip.clicked.connect(dialog.accept)
    layout.addWidget(skip)
    dialog.exec()
    panel.shutdown()
    if first_run:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text('{"schema_version": 1, "shown": true}\n', encoding="utf-8")
