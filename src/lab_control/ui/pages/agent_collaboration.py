"""Workspace settings for the local MCP endpoint."""

from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PySide6.QtCore import QSignalBlocker, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from lab_control.agent.server import AgentServer
from lab_control.ui.pages.mcp_labels import ACTION_LABELS, STATE_LABELS
from lab_control.ui.widgets.popup_surface import WorkspaceMenu


def export_codex_bundle(server, destination: Path):
    connection = destination.with_suffix(".connection.json")
    if getattr(sys, "frozen", False):
        command = sys.executable
        args = ["--agent-connector"]
    else:
        command = sys.executable
        args = ["-m", "lab_control", "--agent-connector"]
    args += ["--connection", str(connection.resolve())]
    snippet = (
        "[mcp_servers.lab_control]\n"
        + "command = "
        + json.dumps(command, ensure_ascii=False)
        + "\n"
        + "args = "
        + json.dumps(args, ensure_ascii=False)
        + "\n"
        + "startup_timeout_sec = 30\ntool_timeout_sec = 90\n"
    )
    server.export_connection(connection)
    try:
        stream = destination.open("x", encoding="utf-8")
    except Exception:
        connection.unlink()
        raise
    try:
        with stream:
            stream.write(snippet)
    except Exception:
        destination.unlink()
        connection.unlink()
        raise


class AgentCollaborationPanel(QWidget):
    completed = Signal(object)

    def __init__(self, runtime, parent=None):
        super().__init__(parent)
        self.runtime, self.service = runtime, runtime.agent
        self.service.desktop_grant_enabled = True
        if self.service.server is None:
            self.service.server = AgentServer(self.service)
        self.server = self.service.server
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mcp-settings")
        self.busy = False
        self.task_rows: dict[str, tuple] = {}

        layout = QVBoxLayout(self)
        header = QHBoxLayout()
        heading = QLabel("MCP 接入")
        heading.setObjectName("panelTitle")
        header.addWidget(heading)
        self.state = QLabel("已停止")
        header.addWidget(self.state)
        self.authority = QLabel()
        self.authority.setObjectName("pageDescription")
        header.addWidget(self.authority)
        header.addStretch()
        self.enabled = QCheckBox("启用服务")
        self.enabled.setAccessibleName("启用本机 MCP 服务")
        header.addWidget(self.enabled)
        layout.addLayout(header)

        controls = QHBoxLayout()
        controls.addWidget(QLabel("本机端口"))
        self.local_port = QSpinBox()
        self.local_port.setAccessibleName("本机端口")
        self.local_port.setRange(1024, 65535)
        self.local_port.setValue(self.server.settings["local_port"])
        controls.addWidget(self.local_port)
        self.apply = QPushButton("应用")
        controls.addWidget(self.apply)
        controls.addSpacing(8)
        self.address = QLabel()
        self.address.setObjectName("pageDescription")
        controls.addWidget(self.address, 1)
        self.copy = QPushButton("复制地址")
        self.copy.clicked.connect(lambda: QApplication.clipboard().setText(self.server.url))
        controls.addWidget(self.copy)
        self.export = QPushButton("导出配置")
        self.export.setObjectName("mcpExportMenu")
        menu = WorkspaceMenu(self.export)
        menu.addAction("连接 JSON", lambda: self.export_config(False))
        menu.addAction("Codex 配置", lambda: self.export_config(True))
        self.export.setMenu(menu)
        controls.addWidget(self.export)
        layout.addLayout(controls)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setObjectName("pageDescription")
        self.status.hide()
        layout.addWidget(self.status)
        self.tasks = QWidget()
        self.rows_layout = QVBoxLayout(self.tasks)
        self.rows_layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.tasks)

        self.completed.connect(self._completed)
        self.enabled.toggled.connect(self.configure)
        self.local_port.valueChanged.connect(self.update_apply)
        self.local_port.lineEdit().returnPressed.connect(self.apply_port)
        self.apply.clicked.connect(self.apply_port)
        self.timer = QTimer(self, interval=1000)
        self.timer.timeout.connect(self.refresh)
        self.refresh()
        if self.server.settings["enabled"] and not self.server.snapshot()["running"]:
            self.configure(True)

    def update_apply(self):
        self.apply.setEnabled(not self.busy and self.local_port.value() != self.server.settings["local_port"])

    def apply_port(self):
        if self.apply.isEnabled():
            self.configure(self.enabled.isChecked())

    def configure(self, enabled):
        if self.busy or self.runtime.closed or self.service.closing:
            return
        self.busy = True
        self.enabled.setEnabled(False)
        self.local_port.setEnabled(False)
        self.export.setEnabled(False)
        self.copy.setEnabled(False)
        self.update_apply()
        self.status.hide()
        settings = {"enabled": enabled, "local_port": self.local_port.value()}
        future = self.worker.submit(self.server.configure, settings)
        future.add_done_callback(lambda f: self.completed.emit(f.exception()))

    def _completed(self, error):
        self.busy = False
        if self.runtime.closed or self.service.closing:
            return
        self.enabled.setEnabled(True)
        self.local_port.setEnabled(True)
        self.copy.setEnabled(True)
        self.export.setEnabled(True)
        if error:
            self.message(str(error))
        self.refresh()

    def message(self, text):
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setText(text)
        self.status.setVisible(bool(text))

    def export_config(self, codex):
        name, file_filter = (
            ("lab-control-codex.toml", "TOML (*.toml)")
            if codex
            else ("lab-control-connection.json", "JSON (*.json)")
        )
        path, _ = QFileDialog.getSaveFileName(self, "导出配置", name, file_filter)
        if path:
            try:
                if codex:
                    export_codex_bundle(self.server, Path(path))
                else:
                    self.server.export_connection(Path(path))
                self.message("已导出 Codex 配置和连接 JSON" if codex else "已导出连接 JSON")
            except OSError as exc:
                self.message(f"导出失败：{exc}")

    def refresh(self):
        if self.runtime.closed or self.service.closing:
            return
        snapshot = self.service.snapshot(active_only=True)
        server = snapshot["server"]
        running = server["running"]
        if not self.busy:
            with QSignalBlocker(self.enabled):
                self.enabled.setChecked(running)
        self.state.setText("运行中" if running else "已停止")
        self.address.setText(self.server.url)
        self.update_apply()
        grant: dict = next((g for g in snapshot["grants"] if g["enabled"] and g.get("desktop_automatic")), {})
        self.authority.setText(f"已自动授权 {len(grant.get('devices', {}))} 台设备")
        active = snapshot["tasks"]
        active_ids = {t["id"] for t in active}
        for key in self.task_rows.keys() - active_ids:
            row, *_ = self.task_rows.pop(key)
            self.rows_layout.removeWidget(row)
            row.hide()
            row.deleteLater()
        self.tasks.setVisible(bool(active))
        for task in active:
            key = task["id"]
            if key not in self.task_rows:
                row = QWidget()
                line = QHBoxLayout(row)
                line.setContentsMargins(0, 2, 0, 2)
                name = QLabel()
                record: dict = next(
                    (r for r in self.runtime.inventory.all() if r["id"] == task["device_id"]), {}
                )
                model = (
                    (record.get("identity") or {}).get("model")
                    or record.get("displayName")
                    or task["device_id"]
                )
                name.setText(f"{model} · {ACTION_LABELS.get(task['action'], task['action'])}")
                line.addWidget(name, 1)
                progress = QLabel()
                line.addWidget(progress)
                stop = QPushButton("停止并保存")
                stop.clicked.connect(lambda _=False, task_id=key: self.service.stop_local(task_id))
                line.addWidget(stop)
                self.rows_layout.addWidget(row)
                self.task_rows[key] = row, progress, stop
            _, progress, stop = self.task_rows[key]
            state = task["state"]
            text = STATE_LABELS.get(state, state)
            counts = task.get("progress") or {}
            if "total" in counts:
                text += f" · {counts['done']}/{counts['total']}"
            elif "recorded_frames" in task:
                text += f" · 已保存 {task['recorded_frames']} 帧"
            progress.setText(text)
            stop.setEnabled(state in {"accepted", "running"} and task["action"] != "stop")

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()
        self.timer.start()

    def hideEvent(self, event):
        self.timer.stop()
        super().hideEvent(event)

    def shutdown(self):
        self.timer.stop()
        self.worker.shutdown(wait=True)
