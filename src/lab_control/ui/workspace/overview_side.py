"""Home page right column: device alerts and running acquisition tasks (read-only views)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from lab_control.application.bulk_connection import device_label
from lab_control.application.catalog import instrument_category
from lab_control.ui.kit import Card, Dot, ElideLabel, Pill

if TYPE_CHECKING:
    from lab_control.ui.real_window import RealWindow

MAX_ROWS = 5


class _Row(QFrame):
    """Clickable list row: status dot, title and detail, right-aligned meta."""

    def __init__(self, object_name: str, on_click) -> None:
        super().__init__()
        self.setObjectName(object_name)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.on_click = on_click
        self.device_id = ""
        row = QHBoxLayout(self)
        row.setContentsMargins(10, 8, 10, 8)
        row.setSpacing(10)
        self.dot = Dot("ok", 7)
        row.addWidget(self.dot, 0, Qt.AlignmentFlag.AlignTop)
        column = QVBoxLayout()
        column.setSpacing(1)
        self.title = ElideLabel()
        self.title.setObjectName("taskTitle")
        self.detail = ElideLabel()
        self.detail.setObjectName("taskMeta")
        column.addWidget(self.title)
        column.addWidget(self.detail)
        row.addLayout(column, 1)
        self.meta = QLabel()
        self.meta.setObjectName("taskTime")
        row.addWidget(self.meta, 0, Qt.AlignmentFlag.AlignTop)

    def set(self, device_id: str, status: str, title: str, detail: str, meta: str, *, live=False) -> None:
        self.device_id = device_id
        self.dot.set_status(status, live)
        for label, text in ((self.title, title), (self.detail, detail), (self.meta, meta)):
            if label.text() != text:
                label.setText(text)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self.device_id:
            self.on_click(self.device_id)
        super().mouseReleaseEvent(event)


class OverviewSide(QWidget):
    def __init__(self, window: RealWindow) -> None:
        super().__init__()
        self.workspace = window
        self.setObjectName("overviewSideColumn")
        self.setFixedWidth(320)
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(14)
        self.alerts = Card("告警", padding=(12, 10, 12, 12), spacing=8, object_name="overviewSide")
        self.alerts.header().addStretch(1)
        self.alert_count = Pill("0 项", "neutral")
        self.alerts.header().addWidget(self.alert_count)
        self.alert_empty = QLabel("暂无设备告警")
        self.alert_empty.setObjectName("taskMeta")
        self.alerts.body.addWidget(self.alert_empty)
        self.alert_rows = [_Row("alarmItem", self.open_device) for _ in range(MAX_ROWS)]
        for row in self.alert_rows:
            self.alerts.body.addWidget(row)
        column.addWidget(self.alerts)
        self.tasks = Card("采集任务", padding=(6, 4, 6, 8), spacing=0, object_name="overviewSide")
        self.tasks.header().addStretch(1)
        self.task_count = Pill("无运行", "neutral")
        self.tasks.header().addWidget(self.task_count)
        self.task_empty = QLabel("没有正在运行的监测或记录")
        self.task_empty.setObjectName("taskMeta")
        self.task_empty.setContentsMargins(10, 8, 10, 4)
        self.tasks.body.addWidget(self.task_empty)
        self.task_rows = [_Row("taskRow", self.open_device) for _ in range(MAX_ROWS)]
        for row in self.task_rows:
            self.tasks.body.addWidget(row)
        column.addWidget(self.tasks)
        column.addStretch(1)

    def open_device(self, device_id: str) -> None:
        window = self.workspace
        record = window.controller.records.get(device_id)
        if not record:
            return
        category = instrument_category(record)
        window.navigate("仪器测控", category, device_id)

    def refresh(self, problems: list[tuple[str, str]], tasks: dict[str, dict[str, Any]]) -> None:
        window = self.workspace
        self.alert_count.set(f"{len(problems)} 项", "err" if problems else "neutral")
        self.alert_empty.setVisible(not problems)
        for row, problem in zip(self.alert_rows, problems + [("", "")] * MAX_ROWS, strict=False):
            device_id, text = problem
            row.setVisible(bool(device_id))
            if device_id:
                name, _, reason = text.partition("：")
                row.set(device_id, "err", name, reason or "连接异常", "")
        running = [
            (device_id, task)
            for device_id, task in tasks.items()
            if task.get("running") and device_id in window.controller.records
        ]
        recording = sum(bool(task.get("recording")) for _, task in running)
        self.task_count.set(
            f"{len(running)} 个运行中" if running else "无运行", "accent" if running else "neutral"
        )
        self.task_empty.setVisible(not running)
        for row, item in zip(self.task_rows, running + [("", {})] * MAX_ROWS, strict=False):
            device_id, task = item
            row.setVisible(bool(device_id))
            if not device_id:
                continue
            record = window.controller.records[device_id]
            interval = task.get("interval")
            actual = task.get("frame_interval")
            detail = " · ".join(
                part
                for part in (
                    f"间隔 {interval:.2f} s"
                    if isinstance(interval, (int, float)) and interval
                    else "收完即采",
                    "记录中" if task.get("recording") else "未记录",
                )
                if part
            )
            row.set(
                device_id,
                "err" if task.get("recording") else "ok",
                device_label(record),
                detail,
                f"{actual:.2f} s/帧" if isinstance(actual, (int, float)) else "",
                live=True,
            )
        self.tasks.setToolTip(f"记录中 {recording} 台" if recording else "")
