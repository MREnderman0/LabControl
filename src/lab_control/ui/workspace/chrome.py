"""Window chrome: sidebar with instrument categories, header (breadcrumb, search, alerts) and status bar."""

from __future__ import annotations

import importlib.metadata
import shutil
import time
from collections import Counter
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QSortFilterProxyModel, QStringListModel, Qt
from PySide6.QtWidgets import (
    QCompleter,
    QFrame,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from lab_control.application.bulk_connection import device_label
from lab_control.application.catalog import instrument_categories, instrument_category
from lab_control.application.controls import CATEGORY_LABELS
from lab_control.discovery.presence import LIVE_CONNECTIONS
from lab_control.ui.kit import (
    Dot,
    ElideLabel,
    IconButton,
    NavButton,
    SearchBox,
    SubNavButton,
)
from lab_control.ui.kit.base import plain_tooltip

if TYPE_CHECKING:
    from lab_control.ui.real_window import RealWindow

try:
    VERSION = importlib.metadata.version("tf-labcontrol-shell")
except importlib.metadata.PackageNotFoundError:  # pragma: no cover - source checkout without metadata
    VERSION = ""


def _gigabytes(path) -> str:
    try:
        target = path
        while not target.exists() and target.parent != target:
            target = target.parent
        return f"{shutil.disk_usage(target).free / 1e9:.0f} GB"
    except OSError:
        return "—"


class WorkspaceChrome:
    """Owns the parts of the window that are not a page; refreshed with the overview timer."""

    def __init__(self, window: RealWindow) -> None:
        self.window = window
        self.category_buttons: dict[str, SubNavButton] = {}
        self._category_signature: tuple = ()
        self._search_entries: list[tuple[str, str]] = []
        self._disk = ("", "—", 0.0)

    # ------------------------------------------------------------------ sidebar
    def build_categories(self) -> QWidget:
        box = QWidget()
        box.setObjectName("sidebarCategories")
        self.category_layout = QVBoxLayout(box)
        self.category_layout.setContentsMargins(0, 1, 0, 3)
        self.category_layout.setSpacing(1)
        self.category_box = box
        box.hide()
        return box

    def build_runtime_badge(self) -> QFrame:
        badge = QFrame()
        badge.setObjectName("runtimeBadge")
        row = QHBoxLayout(badge)
        row.setContentsMargins(9, 7, 10, 7)
        row.setSpacing(6)
        self.runtime_dot = Dot("ok" if self.window.runtime.hardware else "warn", 7)
        row.addWidget(self.runtime_dot)
        self.runtime_text = ElideLabel("真实设备" if self.window.runtime.hardware else "演示版")
        self.runtime_text.setObjectName("runtimeBadgeText")
        row.addWidget(self.runtime_text, 1)
        return badge

    def sync_categories(self) -> None:
        window = self.window
        categories = window._submenu_categories(
            "仪器测控", window.controller.records, window.controller.states
        )
        counts = {category: 0 for category in categories}
        alerts = dict.fromkeys(categories, False)
        for device_id, record in window.controller.records.items():
            state = window.controller.states.get(device_id, {})
            if state.get("connectionState") in LIVE_CONNECTIONS:
                for category in instrument_categories(record):
                    if category in counts:
                        counts[category] += 1
                        alerts[category] |= state.get("connectionState") in {"degraded", "error"}
        signature = tuple(categories)
        if signature != self._category_signature:
            self._category_signature = signature
            for button in self.category_buttons.values():
                self.category_layout.removeWidget(button)
                button.deleteLater()
            self.category_buttons = {}
            for category in categories:
                button = SubNavButton(CATEGORY_LABELS.get(category, category))
                button.clicked.connect(lambda _checked=False, c=category: self.open_category(c))
                self.category_layout.addWidget(button)
                self.category_buttons[category] = button
            self.sync_selection()
        for category, button in self.category_buttons.items():
            button.set_count(counts.get(category, 0), alerts.get(category, False))
        self.category_box.setVisible(bool(self.category_buttons) and not window._manual_sidebar_collapsed)

    def open_category(self, category: str) -> None:
        self.window.navigate("仪器测控", category)

    def current_category(self) -> str:
        window = self.window
        if window.current_module != "仪器测控":
            return ""
        return window.submenu.tabData(window.submenu.currentIndex()) or ""

    def sync_selection(self) -> None:
        window = self.window
        category = self.current_category()
        for name, nav in window.nav_buttons.items():
            nav.setChecked(name == window.current_module and not (name == "仪器测控" and category))
            if name == "仪器测控" and isinstance(nav, NavButton):
                nav.set_parent_active(bool(category))
        for value, item in self.category_buttons.items():
            item.setChecked(value == category)

    # ------------------------------------------------------------------ header
    def build_header_tools(self, row: QHBoxLayout) -> None:
        window = self.window
        row.addStretch()
        self.search = SearchBox("搜索设备、型号或功能槽位", parent=window)
        self.search.setFixedWidth(300)
        self.search_model = QStringListModel(window)
        proxy = QSortFilterProxyModel(window)
        proxy.setSourceModel(self.search_model)
        self.completer = QCompleter(proxy, window)
        self.completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self.completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self.completer.setMaxVisibleItems(10)
        if (popup := self.completer.popup()) is not None:
            popup.setObjectName("searchResults")
        self.search.edit.setCompleter(self.completer)
        self.completer.activated[str].connect(self._search_chosen)  # type: ignore[index]  # str overload
        self.search.edit.returnPressed.connect(lambda: self._search_chosen(self.search.edit.text()))
        row.addWidget(self.search)
        self.alerts = IconButton("bell", "设备告警", 32, 17)
        self.alerts.clicked.connect(self._open_alerts)
        row.addWidget(self.alerts)

    def set_crumbs(self, parent: str, leaf: str) -> None:
        window = self.window
        window.breadcrumb.setText(f"{parent}  /" if parent else "")
        window.breadcrumb.setVisible(bool(parent))
        window.section_title.setText(leaf)

    # ------------------------------------------------------------------ search
    def _refresh_search(self) -> None:
        window = self.window
        slots = window.runtime.repository.function_slots
        entries = []
        for device_id, record in window.controller.records.items():
            identity = record.get("identity") or {}
            slot = record.get("slots", {}).get(window.controller.mode)
            parts = [
                identity.get("model") or record.get("adapterId", ""),
                device_label(record),
                slots.label(slot, window.controller.mode) if slot else "",
                " / ".join(CATEGORY_LABELS.get(role, role) for role in instrument_categories(record)),
            ]
            text = " · ".join(dict.fromkeys(p for p in parts if p))
            entries.append((text, device_id))
        counts = Counter(label for label, _ in entries)
        entries = sorted((f"{label} · {key}" if counts[label] > 1 else label, key) for label, key in entries)
        if entries != self._search_entries:
            self._search_entries = entries
            self.search_model.setStringList([text for text, _ in entries])

    def _search_chosen(self, text: str) -> None:
        window = self.window
        if not text.strip():
            return
        device_id = next((key for label, key in self._search_entries if label == text), "")
        if not device_id:
            matches = [key for label, key in self._search_entries if text.strip().lower() in label.lower()]
            device_id = matches[0] if len(matches) == 1 else ""
        if not device_id:
            return
        record = window.controller.records.get(device_id)
        if record is None:
            return
        category = instrument_category(record)
        self.search.edit.clear()
        self.search.edit.clearFocus()
        window.navigate("仪器测控", category, device_id)

    # ------------------------------------------------------------------ alerts and status bar
    def _problems(self) -> list[tuple[str, str]]:
        window = self.window
        problems = []
        for device_id, state in window.controller.states.items():
            record = window.controller.records.get(device_id)
            if not record:
                continue
            error = state.get("lastError") or record.get("lastError")
            if state.get("connectionState") in {"degraded", "error"} or (
                error and state.get("connectionState") in LIVE_CONNECTIONS
            ):
                problems.append((device_id, f"{device_label(record)}：{error or '连接异常'}"))
        return problems

    def _open_alerts(self) -> None:
        problems = self._problems()
        if problems:
            window = self.window
            window.navigate("仪器测控", "", problems[0][0])
        else:
            self.window.navigate("操作日志")

    def build_status_bar(self) -> QFrame:
        window = self.window
        bar = QFrame()
        bar.setObjectName("appStatusBar")
        bar.setFixedHeight(30)
        row = QHBoxLayout(bar)
        row.setContentsMargins(20, 0, 20, 0)
        row.setSpacing(18)
        online = QHBoxLayout()
        online.setSpacing(4)
        self.status_dot = Dot("ok" if window.runtime.hardware else "warn", 6, box=10)
        online.addWidget(self.status_dot)
        self.status_devices = QLabel()
        self.status_devices.setObjectName("appStatusText")
        online.addWidget(self.status_devices)
        row.addLayout(online)
        self.status_tasks = QLabel()
        self.status_tasks.setObjectName("appStatusText")
        row.addWidget(self.status_tasks)
        self.status_storage = ElideLabel()
        self.status_storage.setObjectName("appStatusText")
        row.addWidget(self.status_storage, 1)
        window.message.setParent(bar)
        window.message.setObjectName("commandStatusBar")
        window.message.setWordWrap(False)
        window.message.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(window.message, 2)
        self.status_version = QLabel(f"LabControl {VERSION}".strip())
        self.status_version.setObjectName("appStatusText")
        row.addWidget(self.status_version)
        return bar

    def refresh(self) -> None:
        window = self.window
        self.sync_categories()
        self._refresh_search()
        states = window.controller.states
        records = window.controller.records
        online = sum(
            states.get(key, {}).get("connectionState") in {"connected", "busy", "degraded", "recovering"}
            for key in records
        )
        kind = "真实设备" if window.runtime.hardware else "模拟演示"
        self.status_devices.setText(f"{kind} · {online} / {len(records)} 在线")
        self.runtime_text.setText(f"{'真实设备' if window.runtime.hardware else '演示版'} · {online} 台在线")
        tasks: dict[str, Any] = window.runtime.monitoring.snapshots()
        running = sum(bool(task.get("running")) for task in tasks.values())
        recording = sum(bool(task.get("recording")) for task in tasks.values())
        self.runtime_dot.set_status("ok" if window.runtime.hardware else "warn", live=bool(running))
        self.status_tasks.setText(
            f"采集中 {running}" + (f" · 记录中 {recording}" if recording else "") if running else "无采集任务"
        )
        root = window.runtime.repository.data_root
        now = time.monotonic()
        if self._disk[0] != str(root) or now - self._disk[2] > 15:
            self._disk = (str(root), _gigabytes(root), now)
        self.status_storage.setText(f"记录目录 {root} · 剩余 {self._disk[1]}")
        problems = self._problems()
        self.alerts.set_badge(bool(problems))
        self.alerts.setToolTip(
            plain_tooltip("\n".join(text for _, text in problems[:5]))
            if problems
            else "没有设备告警 · 点击查看操作日志"
        )

    def apply_collapsed(self, collapsed: bool) -> None:
        self.category_box.setVisible(bool(self.category_buttons) and not collapsed)
