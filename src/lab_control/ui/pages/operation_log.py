"""Read-only operation log: the daily JSONL audit files in the runtime ``logs`` folder."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from lab_control.ui.styles import tokens as tk
from lab_control.ui.widgets import icons
from lab_control.ui.widgets.workspace_combo import WorkspaceComboBox

MAX_ENTRIES = 5000
EVENT_LABELS = {
    "scan_started": "开始扫描",
    "scan_finished": "扫描完成",
    "discovery_result": "发现结果",
    "connected": "已连接",
    "disconnected": "已断开",
    "connection_failed": "连接失败",
    "manual_reconnect_succeeded": "手动重连成功",
    "manual_reconnect_failed": "手动重连失败",
    "serial_autoconnect_error": "串口自动连接异常",
    "bulk_connection_item": "批量连接 · 单台结果",
    "bulk_connection_finished": "批量连接完成",
    "capabilities_refreshed": "能力刷新",
    "capability_publish_failed": "能力发布失败",
    "slot_created": "新建功能槽位",
    "slot_renamed": "重命名功能槽位",
    "slot_deleted": "删除功能槽位",
    "operation_requested": "参数操作请求",
    "operation_completed": "参数操作完成",
    "operation_failed": "参数操作失败",
    "settings_sync_state": "参数同步状态",
    "control_release_failed": "控制释放失败",
    "acquisition_setup_started": "采集准备",
    "acquisition_started": "开始采集",
    "acquisition_statistics": "采集统计",
    "monitor_started": "开始监测",
    "monitor_paused": "暂停监测",
    "monitor_save_timing": "监测保存耗时",
    "monitor_settle_expired": "监测等待超时",
    "recording_completed": "记录完成",
    "capture_save_failed": "单帧保存失败",
    "stream_failed": "数据流失败",
    "stream_cleanup_failed": "数据流清理失败",
    "frame_listener_failed": "帧监听失败",
    "listener_error": "监听异常",
    "cleanup_failed": "清理失败",
    "agent_grant_created": "Agent 授权",
    "agent_grant_revoked": "撤销 Agent 授权",
    "agent_acquisition_started": "Agent 采集",
    "agent_task_finished": "Agent 任务完成",
    "agent_tracking_failed": "Agent 跟踪失败",
    "agent_firewall_requested": "申请防火墙规则",
    "agent_firewall_completed": "防火墙规则完成",
}
CATEGORIES = (
    ("all", "全部"),
    ("device", "设备与连接"),
    ("operation", "参数操作"),
    ("acquisition", "采集与数据"),
    ("agent", "Agent 与网络"),
    ("problem", "异常"),
)
DEVICE_EVENTS = (
    "scan_",
    "discovery_",
    "connect",
    "disconnect",
    "manual_reconnect",
    "serial_",
    "bulk_connection",
    "slot_",
    "capabilit",
)
APPLY_PROBLEMS = {"partial": "部分完成", "uncertain": "结果未确认", "rejected": "已拒绝"}


@dataclass(frozen=True)
class LogEntry:
    timestamp: str
    event: str
    details: dict[str, Any]

    @property
    def category(self) -> str:
        if self.event.startswith("agent_"):
            return "agent"
        if self.event.startswith(DEVICE_EVENTS):
            return "device"
        if self.event.startswith(("operation_", "settings_sync", "control_")):
            return "operation"
        return "acquisition"

    @property
    def apply_result(self) -> dict:
        result = self.details.get("result")
        if self.event == "operation_completed" and isinstance(result, dict) and "outcome" in result:
            return result
        return {}

    @property
    def problem(self) -> bool:
        text = self.event.lower()
        status = str(self.details.get("status", "")).lower()
        return (
            "fail" in text
            or "error" in text
            or bool(self.details.get("error"))
            or str(self.apply_result.get("outcome", "")) in APPLY_PROBLEMS
            or bool(self.apply_result.get("error"))
            or status in {"failed", "error", "rejected"}
        )

    @property
    def label(self) -> str:
        outcome = str(self.apply_result.get("outcome", ""))
        if outcome in APPLY_PROBLEMS:
            return "参数操作 · " + APPLY_PROBLEMS[outcome]
        if self.event in EVENT_LABELS:
            return EVENT_LABELS[self.event]
        if self.event.startswith("history_"):
            action = self.event.removeprefix("history_").removesuffix("_failed")
            return f"历史数据 · {action}" + (" 失败" if self.event.endswith("_failed") else "")
        return self.event

    @property
    def device(self) -> str:
        for key in ("device_id", "device", "label", "slot", "source", "scan_id", "task_id"):
            value = self.details.get(key)
            if isinstance(value, str) and value:
                return value
        return "—"

    @property
    def summary(self) -> str:
        result = self.apply_result
        if result.get("error"):
            return " · ".join(str(item) for item in (result.get("operation"), result["error"]) if item)
        if self.details.get("error"):
            return str(self.details["error"])
        parts = []
        for key, value in self.details.items():
            if key in {"device_id", "device"}:
                continue
            text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
            parts.append(f"{key}={text}")
        return "  ".join(parts)

    @property
    def local_time(self) -> str:
        try:
            return datetime.fromisoformat(self.timestamp).astimezone().strftime("%H:%M:%S")
        except ValueError:
            return self.timestamp


def read_log(path: Path, limit: int = MAX_ENTRIES) -> list[LogEntry]:
    """Newest first; malformed lines are skipped, never fatal."""
    entries = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
    except OSError:
        return []
    for line in lines:
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(raw, dict) and isinstance(raw.get("event"), str):
            timestamp = str(raw.pop("timestamp", ""))
            event = raw.pop("event")
            entries.append(LogEntry(timestamp, event, raw))
    entries.reverse()
    return entries


class LogModel(QAbstractTableModel):
    columns = ("时间", "类别", "事件", "设备 / 对象", "详情")

    def __init__(self, resolve=None, parent=None) -> None:
        super().__init__(parent)
        self.entries: list[LogEntry] = []
        self.resolve = resolve

    def set_entries(self, entries: list[LogEntry]) -> None:
        self.beginResetModel()
        self.entries = entries
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.entries)

    def columnCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.columns)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.columns[section]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        entry = self.entries[index.row()]
        column = index.column()
        if role == Qt.ItemDataRole.ForegroundRole:
            if entry.problem and column in (2, 4):
                return QColor(tk.ERR)
            if column in (0, 1, 3):
                return QColor(tk.TEXT_2)
            return None
        if role == Qt.ItemDataRole.ToolTipRole:
            if column == 4:
                return json.dumps(entry.details, ensure_ascii=False, indent=2, default=str)
            if column == 0:
                return entry.timestamp
        if role not in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
            return None
        device = entry.device
        if column == 3 and self.resolve is not None and device != "—":
            device = self.resolve(device) or device
        return (
            entry.local_time,
            dict(CATEGORIES)["problem"] if entry.problem else dict(CATEGORIES)[entry.category],
            entry.label,
            device,
            entry.summary,
        )[column]


class OperationLogPage(QWidget):
    def __init__(self, repository, parent=None, *, resolve_device=None) -> None:
        """``resolve_device`` maps a device id to a readable name (inventory model / display name)."""
        super().__init__(parent)
        self.setObjectName("operationLog")
        self.resolve_device = resolve_device
        self.folder = Path(repository.root) / "logs"
        self.entries: list[LogEntry] = []
        self.signature: tuple[str, float, int] | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(12)

        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.day = WorkspaceComboBox()
        self.day.setAccessibleName("日志日期")
        self.day.setMinimumWidth(150)
        self.day.currentIndexChanged.connect(lambda _index: self.load(force=True))
        bar.addWidget(self.day)
        group_frame = QFrame()
        group_frame.setObjectName("logFilters")
        filters = QHBoxLayout(group_frame)
        filters.setContentsMargins(3, 3, 3, 3)
        filters.setSpacing(2)
        self.category = QButtonGroup(self)
        for index, (key, label) in enumerate(CATEGORIES):
            button = QPushButton(label)
            button.setObjectName("logFilter")
            button.setCheckable(True)
            button.setChecked(index == 0)
            button.setProperty("category", key)
            self.category.addButton(button, index)
            filters.addWidget(button)
        self.category.idClicked.connect(lambda _id: self.apply_filter())
        bar.addWidget(group_frame)
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索事件、设备或详情")
        self.search.setClearButtonEnabled(True)
        self.search.setMinimumWidth(180)
        self.search.textChanged.connect(lambda _text: self.apply_filter())
        bar.addWidget(self.search, 1)
        refresh = QPushButton("刷新")
        refresh.setProperty("buttonSize", "compact")
        refresh.setIcon(icons.icon("scan", tk.TEXT_2))
        refresh.clicked.connect(lambda: self.load(force=True))
        bar.addWidget(refresh)
        folder = QPushButton("打开日志目录")
        folder.setProperty("buttonSize", "compact")
        folder.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.folder))))
        bar.addWidget(folder)
        layout.addLayout(bar)

        self.summary = QLabel()
        self.summary.setObjectName("overviewNote")
        layout.addWidget(self.summary)
        self.model = LogModel(resolve_device, self)
        self.table = QTableView()
        self.table.setObjectName("operationLogTable")
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(32)
        header = self.table.horizontalHeader()
        for column, width in enumerate((86, 110, 190, 220)):
            self.table.setColumnWidth(column, width)
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)
        layout.addWidget(self.table, 1)
        self.empty = QLabel(self.table.viewport())
        self.empty.setObjectName("watchEmpty")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.hide()

        self.timer = QTimer(self)
        self.timer.setInterval(2000)
        self.timer.timeout.connect(self.load)
        self.refresh_days()

    def refresh_days(self) -> None:
        days = (
            sorted((p.stem for p in self.folder.glob("*.jsonl")), reverse=True)
            if self.folder.is_dir()
            else []
        )
        current = self.day.currentData()
        if [self.day.itemData(i) for i in range(self.day.count())] != days:
            self.day.blockSignals(True)
            self.day.clear()
            for day in days:
                self.day.addItem(
                    day + ("（今天）" if day == datetime.now().strftime("%Y-%m-%d") else ""), day
                )
            index = self.day.findData(current)
            self.day.setCurrentIndex(index if index >= 0 else 0)
            self.day.blockSignals(False)
        self.day.setEnabled(bool(days))

    def load(self, force: bool = False) -> None:
        self.refresh_days()
        day = self.day.currentData()
        path = self.folder / f"{day}.jsonl" if day else None
        try:
            stat = path.stat() if path else None
        except OSError:
            stat = None
        signature = (str(path), stat.st_mtime, stat.st_size) if stat else None
        if not force and signature == self.signature:
            return
        self.signature = signature
        self.entries = read_log(path) if path and stat else []
        self.apply_filter()

    def apply_filter(self) -> None:
        button = self.category.checkedButton()
        category = button.property("category") if button else "all"
        needle = self.search.text().strip().lower()
        shown = [
            e
            for e in self.entries
            if (category == "all" or (e.problem if category == "problem" else e.category == category))
            and (not needle or needle in self.search_text(e))
        ]
        self.model.set_entries(shown)
        problems = sum(e.problem for e in self.entries)
        self.summary.setText(
            f"共 {len(self.entries)} 条"
            + (f" · 异常 {problems} 条" if problems else "")
            + (f" · 显示 {len(shown)} 条" if len(shown) != len(self.entries) else "")
            + f" · 来源 {self.folder}"
            + (f" · 仅显示最近 {MAX_ENTRIES} 条" if len(self.entries) >= MAX_ENTRIES else "")
        )
        self.empty.setText(
            "当天没有符合条件的记录"
            if self.entries
            else "还没有操作日志\n扫描、连接、换槽、采集和 Agent 操作会按日记录在这里"
        )
        self.empty.setGeometry(self.table.viewport().rect())
        self.empty.setVisible(not shown)

    def search_text(self, entry: LogEntry) -> str:
        name = ""
        if self.resolve_device is not None and entry.device != "—":
            name = self.resolve_device(entry.device) or ""
        return f"{entry.event} {entry.label} {entry.device} {name} {entry.summary}".lower()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self.empty.setGeometry(self.table.viewport().rect())

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self.load(force=True)
        self.timer.start()

    def hideEvent(self, event) -> None:  # noqa: N802
        self.timer.stop()
        super().hideEvent(event)
