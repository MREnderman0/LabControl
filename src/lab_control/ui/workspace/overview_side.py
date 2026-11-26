"""Home page right column: pinned watch readings and active acquisitions."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QScrollArea, QSizePolicy, QVBoxLayout, QWidget

from lab_control.application.catalog import instrument_category
from lab_control.application.controls import CATEGORY_LABELS
from lab_control.ui.kit import Card, Dot, ElideLabel, Pill
from lab_control.ui.workspace.pinned_watchlist import PinnedWatchlistCard

if TYPE_CHECKING:
    from lab_control.ui.real_window import RealWindow


def format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _clean_device_title(record: dict[str, Any]) -> str:
    identity = record.get("identity") or {}
    model = identity.get("model") or ""
    display_name = record.get("displayName") or ""
    if display_name and "://" not in display_name and not ("_" in display_name and display_name.islower()):
        return display_name
    if model and not ("_" in model and model.islower()):
        return model
    return CATEGORY_LABELS.get(instrument_category(record), "设备")


class _TaskRow(QFrame):
    """Clickable acquisition task row: pulsing dot, title/detail, right-aligned elapsed timer."""

    def __init__(self, on_click) -> None:
        super().__init__()
        self.setObjectName("taskRow")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.on_click = on_click
        self.device_id = ""
        row = QHBoxLayout(self)
        row.setContentsMargins(10, 8, 10, 8)
        row.setSpacing(10)
        self.dot = Dot("ok", 7)
        row.addWidget(self.dot, 0, Qt.AlignmentFlag.AlignVCenter)
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
        row.addWidget(self.meta, 0, Qt.AlignmentFlag.AlignVCenter)

    def set(
        self,
        device_id: str,
        status: str,
        title: str,
        detail: str,
        meta: str,
        *,
        live: bool = False,
        tooltip: str = "",
    ) -> None:
        self.device_id = device_id
        self.dot.set_status(status, live)
        for label, text in ((self.title, title), (self.detail, detail), (self.meta, meta)):
            if label.text() != text:
                label.setText(text)
        if tooltip and self.toolTip() != tooltip:
            self.setToolTip(tooltip)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self.device_id:
            self.on_click(self.device_id)
        super().mouseReleaseEvent(event)


class OverviewSide(QWidget):
    def __init__(self, window: RealWindow) -> None:
        super().__init__()
        self.workspace = window
        self.setObjectName("overviewSideColumn")
        self.setFixedWidth(360)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Ignored)
        self.column = QVBoxLayout(self)
        self.column.setContentsMargins(0, 0, 0, 0)
        self.column.setSpacing(14)

        self.watch_card = PinnedWatchlistCard(window)
        self.watch_card.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Ignored)
        self.column.addWidget(self.watch_card, 1)

        self.tasks = Card("采集任务", padding=(6, 4, 6, 8), spacing=0, object_name="overviewSide")
        self.tasks.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Ignored)
        self.tasks.header().addStretch(1)
        self.task_count = Pill("无运行", "neutral")
        self.tasks.header().addWidget(self.task_count)
        self.task_empty = QLabel("没有正在运行的监测或记录")
        self.task_empty.setObjectName("taskMeta")
        self.task_empty.setContentsMargins(10, 8, 10, 4)
        self.task_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.tasks.body.addWidget(self.task_empty, 1)
        self.task_scroll = QScrollArea()
        self.task_scroll.setAccessibleName("运行中的采集任务")
        self.task_scroll.setWidgetResizable(True)
        self.task_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        items = QWidget()
        self.task_layout = QVBoxLayout(items)
        self.task_layout.setContentsMargins(0, 0, 0, 0)
        self.task_layout.setSpacing(0)
        self.task_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.task_scroll.setWidget(items)
        self.tasks.body.addWidget(self.task_scroll, 1)
        self.task_rows: list[_TaskRow] = []
        self.column.addWidget(self.tasks, 1)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        # Reserve both viewports from the column geometry, independent of their contents.
        available = max(0, self.contentsRect().height() - self.column.spacing())
        upper = available // 2
        self.watch_card.setFixedHeight(upper)
        self.tasks.setFixedHeight(available - upper)
        self.column.activate()

    def open_device(self, device_id: str) -> None:
        window = self.workspace
        record = window.controller.records.get(device_id)
        if not record:
            return
        category = instrument_category(record)
        window.navigate("仪器测控", category, device_id)

    def refresh(self, tasks: dict[str, dict[str, Any]]) -> None:
        window = self.workspace
        running = [
            (device_id, task)
            for device_id, task in tasks.items()
            if task.get("running") and device_id in window.controller.records
        ]
        # Recording tasks appear first, matching round1.
        running.sort(key=lambda item: (not bool(item[1].get("recording")), item[0]))
        recording = sum(bool(task.get("recording")) for _, task in running)
        self.task_count.set(
            f"{len(running)} 个运行中" if running else "无运行", "accent" if running else "neutral"
        )
        self.task_empty.setVisible(not running)
        self.task_scroll.setVisible(bool(running))
        while len(self.task_rows) < len(running):
            row = _TaskRow(self.open_device)
            self.task_rows.append(row)
            self.task_layout.addWidget(row)
        now = time.monotonic()
        slots = window.runtime.repository.function_slots
        for idx, task_row in enumerate(self.task_rows):
            visible = idx < len(running)
            task_row.setVisible(visible)
            if not visible:
                continue
            device_id, task = running[idx]
            record = window.controller.records[device_id]
            model = _clean_device_title(record)
            slot = record.get("slots", {}).get(window.controller.mode, "")
            slot_label = slots.label(slot, window.controller.mode) if slot else ""
            category = CATEGORY_LABELS.get(instrument_category(record), "采集")
            suffix = (
                slot_label
                if slot and slot_label and slot_label != "未绑定"
                else f"{category}{'记录' if task.get('recording') else '监测'}"
            )
            title = (
                f"{model} · {suffix}"
                if suffix and suffix != model and not suffix.startswith(model)
                else (suffix or model)
            )
            interval = task.get("interval")
            interval_text = (
                f"{interval:.2f} s" if isinstance(interval, (int, float)) and interval > 0 else "收完即采"
            )
            channels = task.get("channels") or ()
            rec_state = task.get("recording_state") or {}
            parts = []
            if len(channels) > 1:
                parts.append(f"{len(channels)} 通道")
            parts.append(interval_text)
            if task.get("recording"):
                committed = rec_state.get("committed")
                parts.append(f"已保存 {committed} 帧" if isinstance(committed, int) else "记录中")
            detail = " · ".join(parts)
            start_ts = (
                task.get("recording_started_at")
                if task.get("recording") and task.get("recording_started_at")
                else task.get("started_at") or 0.0
            )
            elapsed_text = format_duration(now - start_ts) if start_ts and start_ts > 0 else "00:00:00"
            actual = task.get("frame_interval")
            tip = f"{title}\n{detail}" + (
                f"\n实际 {actual:.2f} s/帧" if isinstance(actual, (int, float)) else ""
            )
            task_row.set(
                device_id,
                "err" if task.get("recording") else "ok",
                title,
                detail,
                elapsed_text,
                live=True,
                tooltip=tip,
            )
        self.tasks.setToolTip(f"记录中 {recording} 台" if recording else "")
