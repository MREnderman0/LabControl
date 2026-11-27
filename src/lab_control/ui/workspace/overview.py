from __future__ import annotations

import json
import math
from typing import TYPE_CHECKING, Any

import numpy as np
from PySide6.QtCore import QDateTime, QSize, Qt
from PySide6.QtGui import QAction, QColor
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from lab_control.application.bulk_connection import device_label
from lab_control.application.catalog import instrument_category
from lab_control.application.controls import CATEGORY_LABELS
from lab_control.application.settings_sync import SettingsSync
from lab_control.discovery.addresses import identity_conflict_message
from lab_control.discovery.presence import identity_review_reason, is_current, presence_label
from lab_control.domain.oscilloscope import engineering
from lab_control.domain.readback import boolean_value, usable_reading
from lab_control.instruments.registry import DEVICE_DEFINITIONS, requires_manual_connection
from lab_control.ui.controller import (
    background_read_only,
    display_connection_state,
    display_task,
)
from lab_control.ui.kit import ElideLabel, IconButton, Pill, icons, style_button
from lab_control.ui.kit.base import set_property
from lab_control.ui.styles import tokens as tk
from lab_control.ui.widgets.category_cards import CategoryCard, CategoryCardGrid
from lab_control.ui.widgets.device_pool import DeviceDetails, DevicePool, DeviceRowDelegate
from lab_control.ui.widgets.popup_surface import WorkspaceMenu
from lab_control.ui.widgets.workspace_combo import WorkspaceComboBox
from lab_control.ui.workspace.categories import (
    CATEGORIES,
    CATEGORY_DESCRIPTIONS,
    applicable,
    categories_for,
)
from lab_control.ui.workspace.overview_side import OverviewSide
from lab_control.ui.workspace.status import StatusMessage

if TYPE_CHECKING:
    from lab_control.ui.real_window import RealWindow


class WorkspaceOverview:
    """Category cards, device pool and cached detail presentation."""

    def __init__(self, window: RealWindow) -> None:
        self.window = window

    def _build_overview(self) -> None:
        window = self.window
        root = QWidget()
        root.setObjectName("shellRoot")
        layout = QVBoxLayout(root)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(16)
        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)
        heading_column = QVBoxLayout()
        heading_column.setSpacing(2)
        heading = QLabel("设备总览")
        heading.setObjectName("overviewTitle")
        heading_column.addWidget(heading)
        window.summary = ElideLabel()
        window.summary.setObjectName("overviewNote")
        heading_column.addWidget(window.summary)
        toolbar.addLayout(heading_column)
        toolbar.addSpacing(12)
        # At-a-glance counts aligned with the heading baseline, matching round1.
        window.overview_pills = {
            "online": Pill("在线 0", "ok", dot=True),
            "offline": Pill("未连接 0", "neutral", dot=True),
            "running": Pill("采集中 0", "neutral", dot=True),
        }
        self.alert_count = QPushButton("告警 0")
        self.alert_count.setObjectName("overviewAlertButton")
        self.alert_count.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.alert_count.setFixedHeight(22)
        self.alert_count.setIconSize(QSize(13, 13))
        self.alert_count.setToolTip("查看全局告警")
        self.alert_count.setCursor(Qt.CursorShape.PointingHandCursor)
        self.alert_count.clicked.connect(window.chrome._open_alerts)
        pills = window.overview_pills
        for badge in (pills["online"], pills["offline"], self.alert_count, pills["running"]):
            toolbar.addWidget(badge, 0, Qt.AlignmentFlag.AlignBottom)
        toolbar.addStretch(1)
        window.scan_button = QPushButton("扫描设备")
        window.scan_button.setObjectName("primaryButton")
        window.scan_button.clicked.connect(window.scan)
        window.cancel_button = QPushButton("取消扫描")
        window.cancel_button.clicked.connect(lambda: window.runtime.discovery.cancel())
        window.connect_all_button = QPushButton("一键连接")
        window.connect_all_button.clicked.connect(window.connect_all)
        window.stop_connect_button = QPushButton("停止连接")
        window.stop_connect_button.clicked.connect(window.runtime.cancel_connect_all)
        toolbar.addWidget(window.stop_connect_button, 0, Qt.AlignmentFlag.AlignBottom)
        window.connection_results_button = QPushButton("连接结果")
        window.connection_results_button.clicked.connect(window.show_connection_results)
        toolbar.addWidget(window.connection_results_button, 0, Qt.AlignmentFlag.AlignBottom)
        toolbar.addWidget(window.cancel_button, 0, Qt.AlignmentFlag.AlignBottom)
        # Secondary administrative tools live in a quiet menu so the header stays focused.
        window.more_tools_button = IconButton("more", "设备列表、地址列表与环境诊断", size=30, icon_size=16)
        window.more_tools_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        window.more_tools_menu = WorkspaceMenu(window.more_tools_button)
        window.device_list_button = window.more_tools_menu.addAction("设备列表")
        window.device_list_button.setIcon(icons.icon("list", tk.TEXT_2))
        window.device_list_button.triggered.connect(lambda: window.open_category(""))
        window.address_list_button = window.more_tools_menu.addAction("地址列表")
        window.address_list_button.setIcon(icons.icon("network", tk.TEXT_2))
        window.address_list_button.triggered.connect(window.show_static_addresses)
        window.more_tools_menu.addSeparator()
        diagnose_action = window.more_tools_menu.addAction("环境诊断")
        diagnose_action.setIcon(icons.icon("stethoscope", tk.TEXT_2))
        diagnose_action.triggered.connect(window.diagnose)
        window.more_tools_button.setMenu(window.more_tools_menu)
        toolbar.addWidget(window.more_tools_button, 0, Qt.AlignmentFlag.AlignBottom)
        for button in (
            window.stop_connect_button,
            window.connection_results_button,
            window.cancel_button,
            window.connect_all_button,
        ):
            button.setProperty("buttonSize", "compact")
        style_button(window.connect_all_button, "", "plug")
        toolbar.addWidget(window.connect_all_button, 0, Qt.AlignmentFlag.AlignBottom)
        window.scan_button.setProperty("buttonSize", "compact")
        window.scan_button.setIcon(icons.icon("scan", tk.ACCENT_INK, disabled=tk.TEXT_3))
        window.scan_button.setIconSize(QSize(15, 15))
        toolbar.addWidget(window.scan_button, 0, Qt.AlignmentFlag.AlignBottom)
        layout.addLayout(toolbar)
        window.connection_summary = QLabel()
        window.connection_summary.setWordWrap(True)
        window.connection_summary.setObjectName("overviewNote")
        layout.addWidget(window.connection_summary)
        body = QHBoxLayout()
        body.setSpacing(16)
        cards_column = QVBoxLayout()
        cards_column.setSpacing(0)
        body.addLayout(cards_column, 1)
        window.overview_side = OverviewSide(window)
        body.addWidget(window.overview_side)
        layout.addLayout(body, 1)
        window.overview_empty = QLabel("尚未发现设备\n连接并开启设备后，点击右上角“扫描设备”。")
        window.overview_empty.setObjectName("pageEmpty")
        window.overview_empty.setWordWrap(True)
        window.overview_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        cards_column.addWidget(window.overview_empty, 1)
        window.card_grid = CategoryCardGrid()
        window.cards = {
            category: CategoryCard(
                category, label, CATEGORY_DESCRIPTIONS[category], window.card_grid.widget()
            )
            for category, label in CATEGORIES.items()
        }
        for card in window.cards.values():
            card.hide()
            card.clicked.connect(window.open_category)
        cards_column.addWidget(window.card_grid, 1)
        window.device_pool = DevicePool(root)
        window.device_pool.cards = tuple(window.cards.values())
        window.device_pool.categoryRequested.connect(window.open_category)
        titles = QVBoxLayout()
        titles.setSpacing(4)
        window.pool_title = QLabel("设备池")
        window.pool_title.setObjectName("poolTitle")
        titles.addWidget(window.pool_title)
        window.pool_count = QLabel()
        window.pool_count.setObjectName("poolCaption")
        titles.addWidget(window.pool_count)
        window.device_pool.heading.addLayout(titles, 1)
        window.pool_close = QPushButton("×")
        window.pool_close.setObjectName("poolClose")
        window.pool_close.setAccessibleName("关闭设备池")
        window.pool_close.setToolTip("关闭设备池 · Esc")
        window.pool_close.setFixedSize(32, 32)
        window.pool_close.clicked.connect(window.device_pool.hide)
        window.device_pool.heading.addWidget(window.pool_close, 0, Qt.AlignmentFlag.AlignTop)
        window.presence_filter = WorkspaceComboBox()
        for title, value in (("当前设备", "current"), ("历史与已配置", "history"), ("全部设备", "all")):
            window.presence_filter.addItem(title, value)
        window.presence_filter.currentIndexChanged.connect(window.select_category)
        window.category_filter = WorkspaceComboBox()
        window.category_filter.addItem("全部类别", "")
        for category, title in CATEGORIES.items():
            window.category_filter.addItem(title, category)
        window.category_filter.currentIndexChanged.connect(window.select_category)
        window.filter = WorkspaceComboBox()
        for title, value in (
            ("全部状态", "all"),
            ("已连接", "connected"),
            ("未连接", "disconnected"),
            ("忙碌 / 采集中", "busy"),
            ("未知 / 待补信息", "unknown"),
            ("异常", "error"),
            ("有适配器", "adapter"),
            ("已识别", "identified"),
        ):
            window.filter.addItem(title, value)
        window.filter.currentIndexChanged.connect(window.refresh)
        for combo in (window.presence_filter, window.category_filter, window.filter):
            combo.setMinimumWidth(0)
            combo.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        window.presence_filter.setAccessibleName("设备范围")
        window.category_filter.setAccessibleName("设备类别")
        window.filter.setAccessibleName("设备状态")
        window.pool_list = QFrame()
        window.pool_list.setObjectName("poolListPanel")
        list_layout = QVBoxLayout(window.pool_list)
        list_layout.setContentsMargins(0, 0, 0, 0)
        list_layout.setSpacing(8)
        list_heading = QLabel("设备列表")
        list_heading.setObjectName("poolGroupTitle")
        list_layout.addWidget(list_heading)
        window.tree = QTreeWidget()
        window.tree.setObjectName("poolDeviceList")
        window.tree.viewport().setObjectName("poolListViewport")
        window.tree.setAccessibleName("设备列表")
        window.tree.setColumnCount(6)
        window.tree.setHeaderLabels(["设备 / 型号", "类别", "VISA / 连接地址", "状态", "功能槽位", "操作"])
        window.tree.setRootIsDecorated(False)
        window.tree.setAlternatingRowColors(False)
        window.tree.setUniformRowHeights(True)
        window.tree.header().setStretchLastSection(False)
        for column, width in enumerate((195, 90, 300, 95, 120, 110)):
            window.tree.header().setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
            window.tree.setColumnWidth(column, width)
        window.tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        # The delegate presents name, model, state and slot in one selectable device card.
        for column in (1, 2, 3, 4, 5):
            window.tree.setColumnHidden(column, True)
        window.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        window.tree.setHeaderHidden(True)
        window.tree.setItemDelegate(DeviceRowDelegate(window.tree))
        window.tree.setMouseTracking(True)
        window.tree.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        window.tree.setVerticalScrollMode(QTreeWidget.ScrollMode.ScrollPerPixel)
        window.tree.setMinimumSize(0, 0)
        window.tree.itemSelectionChanged.connect(window.selection_changed)
        list_layout.addWidget(window.tree, 1)
        window.pool_list.setFocusProxy(window.tree)
        window.pool_empty = QLabel("暂无符合条件的设备\n可切换范围查看历史记录")
        window.pool_empty.setObjectName("poolEmpty")
        window.pool_empty.setWordWrap(True)
        window.pool_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        list_layout.addWidget(window.pool_empty, 1)
        window.details = DeviceDetails()
        window.details.rename_slot.clicked.connect(lambda: window.action("rename_slot"))
        window.details.edit_channel_names.clicked.connect(lambda: window.action("channel_names"))
        window.action_buttons = {}
        window.pool_more = QPushButton("更多  ···")
        window.pool_more.setObjectName("poolMore")
        window.pool_more.setProperty("poolAction", True)
        window.pool_more.setProperty("buttonSize", "panel")
        window.pool_menu = WorkspaceMenu(window.pool_more)
        window.pool_menu.setMinimumWidth(196)
        window.pool_more.setMenu(window.pool_menu)
        for key, title in (
            ("connect", "连接"),
            ("disconnect", "断开"),
            ("reconnect", "重连"),
            ("identify", "刷新身份"),
            ("status", "读取状态"),
            ("capabilities", "查看能力"),
            ("stop", "停止采集任务"),
            ("rename", "修改名称"),
            ("slot", "功能槽位"),
            ("resource", "选择主资源"),
            ("configure", "指定适配器"),
            ("diagnostic", "设备诊断"),
            ("manage", "设备控制"),
            ("monitor", "实时监测"),
        ):
            control: QPushButton | QAction
            if key in {"connect", "disconnect", "stop", "slot", "manage", "monitor"}:
                control = QPushButton(title)
                control.setProperty("poolAction", True)
                control.setProperty("buttonSize", "panel")
                control.clicked.connect(lambda _checked=False, key=key: window.action(key))
            else:
                control = window.pool_menu.addAction(title)
                control.triggered.connect(lambda _checked=False, key=key: window.action(key))
                if key in {"status", "resource", "configure"}:
                    window.pool_menu.addSeparator()
            window.action_buttons[key] = control
        main_actions = []
        for key in ("connect", "disconnect", "slot", "manage", "monitor"):
            action_button = window.action_buttons[key]
            assert isinstance(action_button, QPushButton)
            main_actions.append(action_button)
        window.action_buttons["connect"].setObjectName("primaryButton")
        window.action_buttons["stop"].setObjectName("poolStop")
        window.device_pool.set_actions((*main_actions, window.pool_more))
        hint = QLabel("断开软件连接不会关闭仪器物理输出。")
        hint.setObjectName("poolCaption")
        hint.setWordWrap(True)
        hint_row = QHBoxLayout()
        hint_row.addWidget(hint, 1)
        stop_button = window.action_buttons["stop"]
        assert isinstance(stop_button, QPushButton)
        hint_row.addWidget(stop_button)
        window.device_pool.footer.addLayout(hint_row)
        window.device_pool.set_content(
            window.pool_list, window.details, (window.presence_filter, window.category_filter, window.filter)
        )
        window.device_pool.hide()
        window.message = StatusMessage(
            ("真实设备" if window.runtime.hardware else "模拟演示") + " · 扫描不会自动连接或恢复输出"
        )
        window.message.linkActivated.connect(window._actions.prepare_component)
        window.message.setWordWrap(True)
        window.message.setObjectName("commandStatusBar")
        layout.addWidget(window.message)
        window.setCentralWidget(root)

    @staticmethod
    def _extract_number(raw: Any) -> float | None:
        if isinstance(raw, dict):
            raw = raw.get("value")
        if isinstance(raw, (int, float)) and not isinstance(raw, bool) and math.isfinite(float(raw)):
            return float(raw)
        return None

    def _device_readout(
        self,
        record: dict[str, Any],
        connection: str,
        task: dict[str, Any],
        watch_alarms: list[dict[str, str]],
    ) -> tuple[str, str, bool]:
        """Summarize live cached telemetry or readback without issuing any device I/O."""
        device_id = record["id"]
        if connection in {"degraded", "error"}:
            return "error", "异常", True
        if connection == "disconnected":
            return "disconnected", "未连接", False
        if connection in {"connecting", "recovering"}:
            return connection, "连接中" if connection == "connecting" else "重连中", False

        alarm_for_dev = [a for a in watch_alarms if a.get("device_id") == device_id]
        if alarm_for_dev:
            return "error", alarm_for_dev[0]["value"] or f"{len(alarm_for_dev)} 路超限", True

        category = instrument_category(record)
        recording = bool(task.get("recording"))
        running = bool(task.get("running"))

        # 1) Latest cached monitoring frame (zero I/O).
        service = self.window.runtime.monitoring
        latest_frame = None
        with service.lock:
            task_obj = service.tasks.get(device_id)
            if task_obj and task_obj.frames:
                latest_frame = max(task_obj.frames.values(), key=lambda f: f.received_at)

        if recording and category in {"spectrum_analyzer", "oscilloscope", "optical_spectrometer"}:
            return "recording", "记录中", False

        state = self.window.controller.states.get(device_id, {})
        if (
            latest_frame is not None
            and latest_frame.session_id == state.get("sessionId")
            and latest_frame.configuration_version == state.get("configurationVersion")
            and latest_frame.batch.measurements
            and category != "signal_generator"
        ):
            measurements = latest_frame.batch.measurements
            status_key = "recording" if recording else "monitoring" if running else "connected"
            if category == "spectrum_analyzer":
                arr = np.asarray(measurements[0].values, dtype=float)
                finite = arr[np.isfinite(arr)]
                if finite.size:
                    unit = measurements[0].unit
                    return status_key, f"{float(finite.max()):.8g} {unit}".strip().replace("-", "−"), False
            elif category == "oscilloscope":
                cfg = latest_frame.batch.configuration
                interval = cfg.get("sample_interval_s")
                if isinstance(interval, (int, float)) and interval > 0:
                    return status_key, engineering(1.0 / interval, "Sa/s"), False
                first = measurements[0]
                disp = first.metadata.get("scope_display") or {}
                if isinstance(disp.get("seconds_per_div"), (int, float)) and disp["seconds_per_div"] > 0:
                    return status_key, engineering(float(disp["seconds_per_div"]), "s") + "/格", False
                return status_key, f"{np.asarray(first.values).size} 点", False
            elif category == "power_supply":
                channel = measurements[0].channel
                actuals = {
                    m.quantity: m for m in measurements if m.channel == channel and np.asarray(m.values).size
                }
                volts, amps = actuals.get("voltage"), actuals.get("current")
                parts = []
                if volts is not None:
                    v_arr = np.real(np.asarray(volts.values)).astype(float, copy=False).ravel()
                    if v_arr.size and math.isfinite(float(v_arr[-1])):
                        parts.append(f"{float(v_arr[-1]):.3f} {volts.unit or 'V'}")
                if amps is not None:
                    a_arr = np.real(np.asarray(amps.values)).astype(float, copy=False).ravel()
                    if a_arr.size and math.isfinite(float(a_arr[-1])):
                        parts.append(f"{float(a_arr[-1]):.3f} {amps.unit or 'A'}")
                if parts:
                    return status_key, f"CH{channel} 实测 " + " · ".join(parts).replace("-", "−"), False
            else:
                first = measurements[0]
                arr = np.real(np.asarray(first.values)).astype(float, copy=False).ravel()
                finite = arr[np.isfinite(arr)]
                if finite.size:
                    val = float(finite[-1])
                    unit = "°C" if first.unit == "degC" else (first.unit or "")
                    if unit == "W" and val > 0:
                        dbm = 10.0 * math.log10(val * 1000.0)
                        return status_key, f"{dbm:.2f} dBm".replace("-", "−"), False
                    if unit in {"Hz", "V", "A", "s", "W"}:
                        return status_key, engineering(val, unit), False
                    return status_key, f"{val:.2f} {unit}".replace("-", "−").strip(), False

        if recording:
            return "recording", "记录中", False
        if running:
            return "monitoring", "监测中", False

        # 2) Cached settings readback from the session (zero I/O).
        readbacks = self.window.runtime.sessions.readback_frames(device_id)
        if readbacks:
            vals = {
                op: frame.reading.value
                for (ch, op), frame in readbacks.items()
                if ch == 1 and SettingsSync.current(frame, state) and usable_reading(frame.reading, {})
            }
            if category == "signal_generator":
                freq = self._extract_number(vals.get("frequency"))
                out = boolean_value(vals.get("output"))
                parts = []
                if freq is not None:
                    parts.append(engineering(freq, "Hz"))
                if out is not None:
                    parts.append("输出开" if out else "输出关")
                if parts:
                    return "connected", "CH1 · " + " · ".join(parts), False
            elif category == "power_supply":
                v = self._extract_number(vals.get("voltage"))
                i = self._extract_number(vals.get("current"))
                parts = []
                if v is not None:
                    parts.append(f"设定 {v:.3f} V")
                if i is not None:
                    parts.append(f"限流 {i:.3f} A")
                if parts:
                    return "connected", "CH1 · " + " · ".join(parts), False
            elif category == "temperature_controller":
                temp = self._extract_number(vals.get("temperature") or vals.get("target_temperature"))
                if temp is not None:
                    return "connected", f"{temp:.2f} °C".replace("-", "−"), False
            elif category == "electronic_load":
                curr = self._extract_number(vals.get("current"))
                if curr is not None:
                    return "connected", f"{curr:.3f} A", False

        if category in {
            "spectrum_analyzer",
            "oscilloscope",
            "optical_power_meter",
            "optical_spectrometer",
            "frequency_counter",
            "accelerometer",
            "phase_noise_analyzer",
            "network_analyzer",
            "multimeter",
        }:
            return "connected", "待开始", False
        return "connected", "已连接", False

    def _card_devices(
        self,
        records: list[dict[str, Any]],
        tasks: dict[str, dict[str, Any]],
        watch_alarms: list[dict[str, str]],
    ) -> list[tuple[str, str, str, str, bool]]:
        window = self.window
        order = {
            "error": 0,
            "recording": 1,
            "monitoring": 2,
            "busy": 2,
            "connected": 3,
            "connecting": 4,
            "recovering": 4,
            "disconnected": 5,
        }
        rows = []
        for record in records:
            state = window.states.get(record["id"], {})
            connection = display_connection_state(state)
            task = tasks.get(record["id"], {})
            slot = window.runtime.repository.function_slots.label(
                record.get("slots", {}).get(window.controller.mode, ""), window.controller.mode
            )
            raw_model = (
                record["identity"].get("model")
                or (record.get("displayName") if "://" not in (record.get("displayName") or "") else "")
                or record["adapterId"]
                or "未识别设备"
            )
            if "_" in raw_model and raw_model.islower():
                model = CATEGORY_LABELS.get(instrument_category(record), raw_model)
            else:
                model = raw_model
            detail = slot if slot and slot != "未绑定" else "未绑定"
            status_key, readout, is_alert = self._device_readout(record, connection, task, watch_alarms)
            rows.append((order.get(status_key, 9), status_key, model, detail, readout, is_alert))
        rows.sort(key=lambda row: row[0])
        return [row[1:] for row in rows]

    def select_category(self, *_args) -> None:
        window = self.window
        window.active_category = window.category_filter.currentData() or ""
        window.refresh()
        candidates = [row for row in window.rows.values() if not row.isHidden()]
        if candidates:
            current = window.rows.get(window.selected_id)
            window.tree.setCurrentItem(current if current in candidates else candidates[0])
        else:
            window.tree.clearSelection()
            window.selected_id = ""
            window.refresh_details()

    def open_category(self, category: str) -> None:
        window = self.window
        window.category_filter.setCurrentIndex(window.category_filter.findData(category))
        window.filter.setCurrentIndex(0)
        window.select_category()
        window.show_device_pool(window.cards.get(category))

    def show_device_pool(self, trigger: QWidget | None = None) -> None:
        window = self.window
        if window.stack.currentWidget() is not window.overview_page:
            return
        window.device_pool.present(trigger)

    def refresh(self) -> None:
        window = self.window
        if window.closing:
            return
        if hasattr(window, "clock_label"):
            window.clock_label.setText(QDateTime.currentDateTime().toString("HH:mm:ss"))
        window.records = window.controller.records
        window.states = window.controller.states
        window._sync_submenu()
        if hasattr(window, "status_bar"):
            window.chrome.refresh()
        current_records = {
            key: record
            for key, record in window.records.items()
            if is_current(record, window.states.get(key))
            or window.states.get(key, {}).get("connectionRetry", {}).get("phase")
            in {"waiting", "retrying", "cleanup"}
        }
        progress = window.runtime.discovery.progress
        running = window.runtime.discovery.is_scanning
        connection_progress = window.runtime.connection_progress()
        connecting = connection_progress["state"] in {"running", "stopping"}
        window.scan_button.setEnabled(not running and not connecting)
        window.scan_button.setToolTip("正在批量连接，请等待完成或停止连接" if connecting else "")
        window.scan_button.setText("扫描中…" if running else "扫描设备")
        window.cancel_button.setEnabled(running)
        window.cancel_button.setVisible(running)
        batch_devices = any(
            not (window.runtime.hardware and requires_manual_connection(record["adapterId"]))
            for record in current_records.values()
        )
        window.connect_all_button.setEnabled(not running and not connecting and batch_devices)
        window.connect_all_button.setText("连接中…" if connecting else "一键连接")
        window.connect_all_button.setToolTip(
            "正在扫描并核验设备，完成后可连接" if running else "连接当前发现的设备；手动串口设备单独连接"
        )
        window.stop_connect_button.setVisible(connecting)
        window.stop_connect_button.setEnabled(connection_progress["state"] == "running")
        window.connection_results_button.setVisible(bool(connection_progress["items"]) and not connecting)
        window.connection_summary.setVisible(connection_progress["state"] != "idle")
        if connecting:
            prefix = (
                "正在停止，等待在途连接完成" if connection_progress["state"] == "stopping" else "正在并行连接"
            )
            active = connection_progress.get("active", [])
            labels = [item["label"] for item in active]
            detail = "、".join(labels[:2]) + (f" 等 {len(labels)} 台" if len(labels) > 2 else "")
            window.connection_summary.setText(
                f"{prefix} · 已完成 {connection_progress['completed']}/{connection_progress['total']}"
                f" · 进行中 {len(active)} · 排队 {connection_progress.get('queued', 0)}"
                + (f" · {detail}" if detail else "")
            )
            window.connection_summary.setToolTip("\n".join(labels))
        else:
            window.connection_summary.setText(window.connection_result_text(connection_progress))
            window.connection_summary.setToolTip("")
        connected = sum(
            s["connectionState"] in {"connected", "busy", "degraded", "recovering"}
            for s in window.states.values()
        )
        identified = sum(bool(item["identity"]) for item in current_records.values())
        errors = sum(
            bool(item.get("lastError") or window.states.get(item["id"], {}).get("lastError"))
            or window.states.get(item["id"], {}).get("connectionState") in {"error", "degraded"}
            for item in current_records.values()
        )
        finished_at = progress.get("finishedAt") or ""
        scan_clock = ""
        if finished_at:
            parsed = QDateTime.fromString(finished_at, Qt.DateFormat.ISODateWithMs)
            if not parsed.isValid():
                parsed = QDateTime.fromString(finished_at, Qt.DateFormat.ISODate)
            if parsed.isValid():
                scan_clock = parsed.toLocalTime().toString("HH:mm")
        prefix = (
            f"上次扫描 {scan_clock}"
            if scan_clock
            else ("真实设备" if window.runtime.hardware else "模拟演示")
        )
        window.summary.setText(f"{prefix} · 发现 {len(current_records)} 台设备")
        tasks = window.runtime.monitoring.snapshots()
        acquiring = sum(bool(task.get("running")) for task in tasks.values())
        watch_alarms = window.chrome.watchlist_alarms() if hasattr(window, "chrome") else []
        total_alarms = errors + len(watch_alarms)
        pills = window.overview_pills
        pills["online"].set(f"在线 {connected}", "ok" if connected else "neutral")
        pills["offline"].set(f"未连接 {max(0, len(current_records) - connected)}", "neutral")
        alarm_text = f"告警 {total_alarms}"
        if self.alert_count.text() != alarm_text or self.alert_count.icon().isNull():
            self.alert_count.setText(alarm_text)
            set_property(self.alert_count, "alert", bool(total_alarms))
            self.alert_count.setIcon(icons.icon("alert", tk.ERR if total_alarms else tk.TEXT_2))
            self.alert_count.setMinimumWidth(self.alert_count.sizeHint().width())
        pills["running"].set(
            f"采集中 {acquiring}", "accent" if acquiring else "neutral", live=bool(acquiring)
        )
        window.overview_side.refresh(tasks)
        window.summary.setToolTip(
            f"发现 {len(current_records)} 台 · 在线 {connected} 台 · 异常 {errors} 台 · 已识别 {identified} 台"
            f" · 历史与已配置 {len(window.records) - len(current_records)} 台"
            f"\n最近扫描：{finished_at or '尚未扫描'}（本次结果 {len(progress.get('items', []))}）"
        )
        current_filter = window.filter.currentData()
        presence_scope = window.presence_filter.currentData()
        for removed_id in set(window.rows) - window.records.keys():
            item = window.rows.pop(removed_id)
            window.tree.takeTopLevelItem(window.tree.indexOfTopLevelItem(item))
        visible_cards = []
        for category in CATEGORIES:
            records = [item for item in current_records.values() if category in categories_for(item)]
            history_count = sum(category in categories_for(item) for item in window.records.values()) - len(
                records
            )
            live = sum(
                window.states.get(item["id"], {}).get("connectionState")
                in {"connected", "busy", "degraded", "recovering"}
                for item in records
            )
            known = sum(bool(item["identity"]) for item in records)
            bound = sum(bool(item.get("slots", {}).get(window.controller.mode)) for item in records)
            abnormal = sum(
                bool(item.get("lastError"))
                or window.states.get(item["id"], {}).get("connectionState") in {"error", "degraded"}
                for item in records
            )
            index = window.category_filter.findData(category)
            category_text = f"{CATEGORIES[category]}  {live}/{len(records)}"
            if window.category_filter.itemText(index) != category_text:
                window.category_filter.setItemText(index, category_text)
            tooltip = (
                f"发现 {len(records)} · 识别 {known} · 连接 {live} · 异常 {abnormal} · 绑定 {bound}"
                f" · 历史与已配置 {history_count}"
            )
            if window.category_filter.itemData(index, Qt.ItemDataRole.ToolTipRole) != tooltip:
                window.category_filter.setItemData(index, tooltip, Qt.ItemDataRole.ToolTipRole)
            window.cards[category].update_counts(len(records), live, abnormal, bound)
            window.cards[category].set_devices(self._card_devices(records, tasks, watch_alarms))
            if records:
                visible_cards.append(window.cards[category])
        window.card_grid.set_cards(tuple(visible_cards))
        window.card_grid.setVisible(bool(visible_cards))
        window.overview_empty.setVisible(not visible_cards)
        for device_id, record in window.records.items():
            if device_id not in window.rows:
                row = QTreeWidgetItem(window.tree)
                row.setData(0, Qt.ItemDataRole.UserRole, device_id)
                window.rows[device_id] = row
            row = window.rows[device_id]
            state = window.states.get(device_id, {})
            connection = display_connection_state(state)
            name = device_label(record)
            status_labels = {
                "connected": "已连接",
                "disconnected": "未连接",
                "busy": "忙碌",
                "connecting": "连接中",
                "recovering": "自动重连中",
                "degraded": "异常",
                "error": "异常",
            }
            colors = {
                "connected": tk.OK,
                "busy": tk.WARN,
                "connecting": tk.WARN,
                "recovering": tk.WARN,
                "degraded": tk.ERR,
                "error": tk.ERR,
            }
            texts = [
                name
                + "\n"
                + " · ".join(
                    value
                    for value in (
                        record["identity"].get("manufacturer") or "身份待确认",
                        record["identity"].get("model", ""),
                    )
                    if value
                ),
                CATEGORY_LABELS.get(instrument_category(record), "未分类"),
                record["config"]["resource"],
                "● "
                + (
                    state["connectionRetry"]["label"]
                    if state.get("connectionRetry", {}).get("label")
                    else "待核对"
                    if connection == "disconnected" and identity_review_reason(record)
                    else "探测失败"
                    if connection == "disconnected" and record.get("presenceState") == "unreachable"
                    else status_labels.get(connection, connection)
                    if connection != "disconnected" or device_id in current_records
                    else presence_label(record, state)
                ),
                window.controller.slot_notice(record["id"])
                or window.runtime.repository.function_slots.label(
                    record.get("slots", {}).get(window.controller.mode, ""), window.controller.mode
                ),
            ]
            badges_changed = row.text(3) != texts[3] or row.text(4) != texts[4]
            for index, text in enumerate(texts):
                if row.text(index) != text:
                    row.setText(index, text)
                    row.setToolTip(index, text)
            fallback_color = tk.ERR if record.get("presenceState") == "unreachable" else tk.TEXT_3
            row.setForeground(3, QColor(colors.get(connection, fallback_color)))
            task = display_task(state)
            row.setToolTip(3, texts[3] + (" / " + task if task else ""))
            row.setToolTip(
                0,
                texts[0]
                + "\n"
                + record["identity"].get("serial", "")
                + f"\n连接状态：{row.toolTip(3).removeprefix('● ')}"
                + f"\n功能槽位：{texts[4]}",
            )
            if badges_changed:
                # Both badges come from hidden columns; invalidate the visible card too.
                window.tree.viewport().update(window.tree.visualItemRect(row))
            show = not window.active_category or window.active_category in categories_for(record)
            if presence_scope == "current":
                show &= device_id in current_records
            elif presence_scope == "history":
                show &= device_id not in current_records
            if current_filter == "connected":
                show &= connection in {"connected", "busy", "degraded", "recovering"}
            elif current_filter in {"disconnected", "busy"}:
                show &= connection == current_filter
            elif current_filter == "unknown":
                show &= not bool(record["identity"])
            elif current_filter == "error":
                show &= bool(record.get("lastError") or state.get("lastError"))
            elif current_filter == "adapter":
                show &= bool(record["adapterId"])
            elif current_filter == "identified":
                show &= bool(record["identity"])
            row.setHidden(not show)
        visible_count = sum(not row.isHidden() for row in window.rows.values())
        window.pool_title.setText(CATEGORIES.get(window.active_category, "全部类别") + " · 设备池")
        window.pool_count.setText(f"{window.presence_filter.currentText()} · {visible_count} 台符合筛选")
        window.tree.setVisible(bool(visible_count))
        window.pool_empty.setVisible(not visible_count)
        if window.selected_id and (
            window.selected_id not in window.rows or window.rows[window.selected_id].isHidden()
        ):
            window.tree.clearSelection()
            window.selected_id = ""
        window.refresh_details()

    def selection_changed(self) -> None:
        window = self.window
        selected = window.tree.selectedItems()
        if selected:
            window.selected_id = selected[0].data(0, Qt.ItemDataRole.UserRole)
        window.refresh_details()

    def open_details(self, device_id: str) -> None:
        window = self.window
        row = window.rows.get(device_id)
        if row is None:
            return
        categories = categories_for(window.records[device_id])
        if window.active_category and window.active_category not in categories:
            category = categories[0]
            window.category_filter.setCurrentIndex(window.category_filter.findData(category))
        if row.isHidden():
            window.filter.setCurrentIndex(window.filter.findData("all"))
        if row.isHidden():
            window.presence_filter.setCurrentIndex(window.presence_filter.findData("all"))
        window.tree.setCurrentItem(row)
        window.tree.scrollToItem(row)
        window.selected_id = device_id
        window.show_device_pool()
        window.refresh_details()

    def refresh_details(self) -> None:
        window = self.window
        record = window.records.get(window.selected_id)
        definition = DEVICE_DEFINITIONS.get((record or {}).get("adapterId", ""))
        window.details.rename_slot.setVisible(
            bool((record or {}).get("slots", {}).get(window.controller.mode))
        )
        window.details.edit_channel_names.setVisible(bool(definition and definition.nameable_channels))
        state = window.states.get(window.selected_id, {})
        busy = bool(state.get("pending")) or state.get("connectionState") in {"busy", "connecting"}
        scanning = window.runtime.discovery.is_scanning
        connecting = window.runtime.connection_progress()["state"] in {"running", "stopping"}
        for key, button in window.action_buttons.items():
            if key == "manage":
                button.setText("打开设备工作页")
                button.setVisible(bool(record and instrument_category(record) in CATEGORY_LABELS))
            elif key == "monitor":
                button.setVisible(False)
            enabled = bool(record)
            if key == "connect":
                enabled &= not bool(state) and bool(
                    record and record["adapterId"] and record["discoveryState"] != "conflict"
                )
            elif key in {"disconnect", "reconnect"}:
                # Teardown queues behind a single background read. The session
                # manager reserves closing atomically and still rejects controls.
                drainable = (
                    state.get("backgroundRead")
                    and state.get("pending") == 1
                    and state.get("connectionState") != "connecting"
                    and not any(
                        state.get(flag)
                        for flag in ("controlPending", "controlGroup", "monitoring", "recording")
                    )
                )
                enabled &= bool(state) and (not busy or bool(drainable))
                if key == "disconnect" and state.get("connectionRetry", {}).get("phase") in {
                    "opening",
                    "retrying",
                    "waiting",
                }:
                    enabled = not state.get("monitoring") and not state.get("recording")
            elif key in {"identify", "status"}:
                enabled &= state.get("connectionState") == "connected" and not busy
            elif key == "capabilities":
                enabled &= bool(record and record["adapterId"])
            elif key == "slot":
                enabled &= (
                    not busy
                    and not any(
                        state.get(flag)
                        for flag in ("controlsLocked", "controlPending", "controlGroup", "monitoring")
                    )
                    and bool(
                        record
                        and applicable(record, window.controller.mode)
                        and record["adapterId"]
                        and not record.get("identityConflict")
                        and record["discoveryState"] != "conflict"
                    )
                )
            elif key == "resource":
                enabled &= not bool(state)
            elif key == "configure":
                enabled &= not bool(state) and not bool(record and record.get("identityConflict"))
            elif key == "stop":
                enabled &= bool(
                    state.get("monitoring") or state.get("activeTask") == "continuous_acquisition"
                )
            elif key in {"manage", "monitor"}:
                enabled &= bool(
                    record
                    and instrument_category(record) in CATEGORY_LABELS
                    and state.get("connectionState") in {"connected", "busy", "degraded", "recovering"}
                )
            if key in {"connect", "reconnect", "resource", "configure"}:
                enabled &= not scanning
            if key in {"connect", "disconnect", "reconnect", "resource", "configure"}:
                enabled &= not connecting
            button.setEnabled(enabled)
            button.setToolTip(
                "正在批量连接，请等待完成或停止连接"
                if connecting and key in {"connect", "disconnect", "reconnect", "resource", "configure"}
                else "正在扫描并核验设备，完成后可连接"
                if scanning and key in {"connect", "reconnect", "resource", "configure"}
                else "请先连接设备"
                if key in {"manage", "monitor"} and not enabled
                else "设备忙碌，请先停止任务"
                if busy and not background_read_only(state)
                else ""
            )
        window.action_buttons["connect"].setVisible(not bool(state))
        window.action_buttons["disconnect"].setVisible(bool(state))
        window.action_buttons["stop"].setVisible(window.action_buttons["stop"].isEnabled())
        window.device_pool.reflow_actions()
        window.pool_more.setEnabled(bool(record))
        if record:
            identity, config = record["identity"], record["config"]
            connection = display_connection_state(state)
            if record["discoveryState"] == "conflict":
                identity_state = "身份冲突"
            elif identity_review_reason(record) and connection == "disconnected":
                identity_state = "待核对"
            elif identity:
                identity_state = "已识别" if is_current(record, state) else "历史身份已保留"
            else:
                identity_state = "尚未识别"
            seen = QDateTime.fromString(record.get("lastSeenAt") or "", Qt.DateFormat.ISODateWithMs)
            probe = record.get("probeReason") or ""
            driver = record.get("driverStatus") or ""
            if driver == "error" and record.get("probeErrorCode") != "driver_missing":
                # Historical records used "error" for every probe failure.
                driver = "unconfirmed"
            configured_serial = config.get("expected_serial", "")
            displayed_serial = identity.get("serial") or configured_serial or "未提供"
            retry = state.get("connectionRetry", {})
            fields = {
                **{
                    key: displayed_serial if key == "serial" else identity.get(key) or "未提供"
                    for key in ("manufacturer", "model", "serial", "firmware")
                },
                "confidence": "模拟身份"
                if identity.get("confidence") == "simulated"
                else identity.get("confidence") or "仅资源描述",
                "acceptance": "待实机验证",
                "connection": retry.get("label")
                or {
                    "connected": "已连接",
                    "disconnected": "未连接",
                    "busy": "忙碌 / 采集中",
                    "connecting": "连接中",
                    "degraded": "连接异常",
                    "recovering": "自动重连中",
                    "error": "连接错误",
                }.get(connection, connection),
                "task": display_task(state) or "空闲",
                "driver": {
                    "enumerated": "已枚举",
                    "error": "运行库不可用",
                    "unconfirmed": "待核对（探测失败）",
                }.get(driver, driver or "待检查"),
                "adapter": record["adapterId"] or "待补协议",
                "resource": f"{config['transport'].upper()}  ·  {config['resource']}",
                "aliases": "\n".join(r for r in record["resources"] if r != config["resource"]) or "无",
                "mode": "通用工作区",
                "slot": window.controller.slot_notice(record["id"])
                or window.runtime.repository.function_slots.label(
                    record.get("slots", {}).get(window.controller.mode, ""), window.controller.mode
                ),
                "presence": presence_label(record, state),
                "identity_state": identity_state,
                "seen": seen.toLocalTime().toString("yyyy-MM-dd HH:mm:ss")
                if seen.isValid()
                else record.get("lastSeenAt") or "尚无发现记录",
                "probe": identity_review_reason(record)
                or {
                    "identified": "身份探测成功",
                    "probe_failed": "身份探测失败",
                    "pending_probe": "等待探测",
                    "metadata_only": "仅有资源描述",
                    "cached_active_session": "复用活动会话",
                    "cached_session_unconfirmed": "会话尚未确认",
                    "sdk_identity_requires_explicit_connection": "连接时核验身份",
                    "manual_connection_required": "需手动选择串口",
                    "not_seen": "本次未发现",
                    "scan_cancelled": "扫描已取消",
                    "enumeration_unconfirmed": "枚举尚未确认",
                    "address_reassigned": "地址已分配给其他设备",
                }.get(probe, probe or "尚未确认"),
                "error": identity_conflict_message(record)
                if record.get("identityConflict") or record["discoveryState"] == "conflict"
                else (
                    f"{retry['label']} · 重试 {retry['attempts']} 次 · {retry['error']}"
                    if retry.get("label")
                    else str(state.get("lastError") or record.get("lastError") or "无")
                ),
                "notice": "\n".join(
                    filter(
                        None,
                        (state.get("acquisitionPreparation", {}).get("message"), retry.get("lastRecovery")),
                    )
                ),
                "readback": json.dumps(state["lastStatus"], ensure_ascii=False, indent=2)
                if state.get("lastStatus")
                else "暂无状态读回",
            }
            window.details.set_fields(
                record["displayName"] or identity.get("model") or "待识别设备",
                f"{presence_label(record, state)}  ·  {identity.get('manufacturer') or '身份待确认'}",
                fields,
            )
        else:
            window.details.set_fields(
                "选择一台设备", "从列表选择设备；未接入的设备可在“历史与已配置”中查看。", {}
            )
