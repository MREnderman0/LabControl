from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable

from PySide6.QtCore import QObject, QRect, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QDesktopServices, QScreen
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QTabBar,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from lab_control.application.runtime import ApplicationRuntime
from lab_control.ui.controller import (
    WorkspaceController,
)
from lab_control.ui.kit.base import NavButton, Pill
from lab_control.ui.pages.monitoring.dashboard import MonitoringDashboard
from lab_control.ui.widgets.category_cards import CategoryCard, CategoryCardGrid
from lab_control.ui.widgets.device_pool import DeviceDetails, DevicePool
from lab_control.ui.widgets.input_wheel_guard import install_input_wheel_guard
from lab_control.ui.widgets.popup_surface import WorkspaceMenu
from lab_control.ui.widgets.workspace_combo import WorkspaceComboBox
from lab_control.ui.workspace.actions import WorkspaceActions
from lab_control.ui.workspace.categories import (
    CATEGORIES as CATEGORIES,
)
from lab_control.ui.workspace.categories import (
    CATEGORY_DESCRIPTIONS as CATEGORY_DESCRIPTIONS,
)
from lab_control.ui.workspace.categories import (
    applicable as applicable,
)
from lab_control.ui.workspace.categories import (
    category_for as category_for,
)
from lab_control.ui.workspace.chrome import WorkspaceChrome
from lab_control.ui.workspace.dialogs import ConnectionDialogs
from lab_control.ui.workspace.navigation import WorkspaceNavigation
from lab_control.ui.workspace.overview import WorkspaceOverview
from lab_control.ui.workspace.overview_side import OverviewSide
from lab_control.ui.workspace.shutdown import WindowShutdown

SIDEBAR_WIDTH = 232  # Layout tests and review scripts pin the expanded sidebar width.
NAV_ITEMS = (
    ("首页", "home"),
    ("仪器测控", "sliders"),
    ("监视列表", "eye"),
    ("数据处理", "chart"),
    ("操作日志", "log"),
    ("系统设置", "gear"),
)


class ServiceBridge(QObject):
    completed = Signal(str, object)
    failed = Signal(str)

    def __init__(self, parent: QObject) -> None:
        super().__init__(parent)
        self.worker = ThreadPoolExecutor(max_workers=2, thread_name_prefix="desktop-service")

    def watch(self, future: Future[Any], label: str) -> None:
        def finish(task):
            try:
                self.completed.emit(label, task.result())
            except Exception as exc:
                self.failed.emit(str(exc))

        future.add_done_callback(finish)


class WorkspaceStack(QStackedWidget):
    """Inactive, larger forms must not impose their minimum width on the dashboard."""

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return QSize(0, 0)


class RealWindow(QMainWindow):
    # Public widget references stay compatible; WorkspaceOverview constructs them.
    scan_button: QPushButton
    cancel_button: QPushButton
    connect_all_button: QPushButton
    stop_connect_button: QPushButton
    connection_results_button: QPushButton
    address_list_button: QPushButton
    device_list_button: QPushButton
    pool_close: QPushButton
    pool_more: QPushButton
    summary: QLabel
    overview_empty: QLabel
    connection_summary: QLabel
    pool_title: QLabel
    pool_count: QLabel
    pool_empty: QLabel
    message: QLabel
    presence_filter: WorkspaceComboBox
    category_filter: WorkspaceComboBox
    filter: WorkspaceComboBox
    card_grid: CategoryCardGrid
    cards: dict[str, CategoryCard]
    device_pool: DevicePool
    pool_list: QFrame
    tree: QTreeWidget
    details: DeviceDetails
    action_buttons: dict[str, QPushButton | QAction]
    pool_menu: WorkspaceMenu
    overview_pills: dict[str, Pill]
    overview_side: OverviewSide

    def __init__(self, runtime: ApplicationRuntime) -> None:
        super().__init__()
        self._input_wheel_guard = install_input_wheel_guard()
        self.runtime = runtime
        self.controller = WorkspaceController(runtime, self)
        self.bridge = ServiceBridge(self)
        self.bridge.completed.connect(self._completed)
        self.bridge.failed.connect(self._failed)
        self.selected_id = ""
        self.active_category = ""
        self.rows: dict[str, QTreeWidgetItem] = {}
        self.records: dict[str, dict[str, Any]] = {}
        self.states: dict[str, dict[str, Any]] = {}
        self.closing = False
        self._shutdown_started = False
        self._manual_sidebar_collapsed = False
        self._watched_screen: QScreen | None = None
        self._screen_connected = False
        self._submenu_signature: tuple[str, tuple[str, ...]] = ("", ())
        self._static_address_dialog: QDialog | None = None
        self._static_address_status: QLabel | None = None
        self._refresh_static_address_dialog: Callable[[], None] | None = None
        self.setWindowTitle("LabControl · 分类卡片 · " + ("真实设备" if runtime.hardware else "模拟演示"))
        self.resize(1440, 900)
        self.setMinimumSize(1280, 760)
        self._navigation = WorkspaceNavigation(self)
        self._overview = WorkspaceOverview(self)
        self._actions = WorkspaceActions(self)
        self._dialogs = ConnectionDialogs(self)
        self._shutdown = WindowShutdown(self)
        self.chrome = WorkspaceChrome(self)
        self._build()
        self.fit_to_screen()
        self.controller.message.connect(self._failed)
        self.controller.navigate.connect(self.navigate)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._overview.refresh)
        self.timer.start(300)
        self.refresh()

    def _build(self) -> None:
        self._build_overview()
        self.overview_page = self.takeCentralWidget()
        assert self.overview_page is not None
        self.page_map: dict[tuple[str, str], QWidget] = {}
        self.current_module = "首页"
        root = QWidget()
        root.setObjectName("cardWorkspace")
        row = QHBoxLayout(root)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        self.sidebar = QFrame()
        self.sidebar.setObjectName("workspaceSidebar")
        self.sidebar.setFixedWidth(SIDEBAR_WIDTH)
        side = QVBoxLayout(self.sidebar)
        side.setContentsMargins(10, 16, 10, 12)
        side.setSpacing(2)
        brand_row = QHBoxLayout()
        brand_row.setContentsMargins(8, 0, 4, 14)
        brand_row.setSpacing(10)
        self.brand_logo = QLabel("TF")
        self.brand_logo.setObjectName("brandLogo")
        self.brand_logo.setFixedSize(30, 30)
        self.brand_logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        brand_row.addWidget(self.brand_logo)
        brand_text = QVBoxLayout()
        brand_text.setSpacing(0)
        self.brand = QLabel("LabControl")
        self.brand.setObjectName("commandBrand")
        brand_text.addWidget(self.brand)
        self.brand_subline = QLabel("实验室测控工作站")
        self.brand_subline.setObjectName("commandEyebrow")
        brand_text.addWidget(self.brand_subline)
        brand_row.addLayout(brand_text, 1)
        side.addLayout(brand_row)
        self.sidebar_section = QLabel("工作区")
        self.sidebar_section.setObjectName("sidebarSection")
        self.sidebar_section.setContentsMargins(4, 0, 0, 6)
        side.addWidget(self.sidebar_section)
        self.nav_buttons = {}
        for module, icon_name in NAV_ITEMS:
            button = NavButton(module, icon_name)
            button.clicked.connect(lambda _checked=False, value=module: self.navigate(value, "", ""))
            self.nav_buttons[module] = button
            if module == "系统设置":
                side.addStretch()
            side.addWidget(button)
            if module == "仪器测控":
                # Connected instrument categories, one click each (replaces the old category tab strip).
                side.addWidget(self.chrome.build_categories())
        side.addSpacing(8)
        self.runtime_badge = self.chrome.build_runtime_badge()
        side.addWidget(self.runtime_badge)
        row.addWidget(self.sidebar)
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)
        header = QFrame()
        header.setObjectName("commandHeader")
        header.setFixedHeight(56)
        top = QHBoxLayout(header)
        top.setContentsMargins(24, 0, 16, 0)
        top.setSpacing(10)
        self.breadcrumb = QLabel()
        self.breadcrumb.setObjectName("commandBreadcrumb")
        top.addWidget(self.breadcrumb)
        self.section_title = QLabel("首页")
        self.section_title.setObjectName("commandModuleTitle")
        top.addWidget(self.section_title)
        top.addSpacing(10)
        self.chrome.build_header_tools(top)
        self.clock_label = QLabel()
        self.clock_label.setObjectName("commandClock")
        top.addWidget(self.clock_label)
        content_layout.addWidget(header)
        self.submenu = QTabBar()
        self.submenu.setObjectName("commandSubnav")
        self.submenu.setExpanding(False)
        self.submenu.setDrawBase(False)
        self.submenu.setUsesScrollButtons(True)
        self.submenu.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.submenu.currentChanged.connect(self.show_page)
        # The tab bar stays the page model; it is shown only where tabs are the navigation (数据处理).
        self.subnav = QWidget()
        self.subnav.setObjectName("commandSubnavRow")
        subnav = QHBoxLayout(self.subnav)
        subnav.setContentsMargins(16, 0, 16, 0)
        subnav.addWidget(self.submenu, 1)
        content_layout.addWidget(self.subnav)
        self.stack = WorkspaceStack()
        stack_layout = self.stack.layout()
        assert stack_layout is not None
        stack_layout.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        self.stack.addWidget(self.overview_page)
        self.dashboard = MonitoringDashboard(self.controller)
        self.dashboard.open_card.connect(self.focus_card)
        self.stack.addWidget(self.dashboard)
        content_layout.addWidget(self.stack, 1)
        self.status_bar = self.chrome.build_status_bar()
        content_layout.addWidget(self.status_bar)
        row.addWidget(content, 1)
        self.settings_page = QScrollArea()
        self.settings_page.setObjectName("settingsPage")
        self.settings_page.setWidgetResizable(True)
        self.settings_page.setFrameShape(QFrame.Shape.NoFrame)
        settings_body = QWidget()
        settings_body.setObjectName("settingsBody")
        self.settings_page.setWidget(settings_body)
        outer = QHBoxLayout(settings_body)
        outer.setContentsMargins(12, 14, 12, 20)
        column = QWidget()
        column.setMaximumWidth(1120)
        outer.addWidget(column, 1)
        settings = QVBoxLayout(column)
        settings.setContentsMargins(0, 0, 0, 0)
        settings.setSpacing(14)
        heading = QLabel("软件设置")
        heading.setObjectName("pageTitle")
        settings.addWidget(heading)
        jumps = QHBoxLayout()
        jumps.setSpacing(6)
        sections: dict[str, QFrame] = {}

        def section(key: str, widget: QWidget) -> QFrame:
            frame = QFrame()
            frame.setObjectName("settingsSection")
            box = QVBoxLayout(frame)
            box.setContentsMargins(14, 12, 14, 14)
            box.addWidget(widget)
            settings.addWidget(frame)
            sections[key] = frame
            return frame

        for key, label in (
            ("storage", "数据存储"),
            ("directory", "库存与日志"),
            ("agent", "Agent 协作"),
            ("environment", "环境与修复"),
        ):
            jump = QPushButton(label)
            jump.setObjectName("settingsJump")
            jump.clicked.connect(
                lambda _checked=False, k=key: self.settings_page.ensureWidgetVisible(sections[k], 0, 0)
            )
            jumps.addWidget(jump)
        jumps.addStretch()
        settings.addLayout(jumps)
        from lab_control.ui.pages.storage_settings import StorageSettingsPanel

        self.storage_panel = StorageSettingsPanel(self.runtime.repository, settings_body)
        section("storage", self.storage_panel)
        directory_row = QWidget()
        directory_layout = QHBoxLayout(directory_row)
        directory_layout.setContentsMargins(0, 0, 0, 0)
        directory_text = QVBoxLayout()
        directory_title = QLabel("软件库存与日志目录")
        directory_title.setObjectName("panelTitle")
        directory_text.addWidget(directory_title)
        directory = QLabel("软件库存与日志目录：" + str(self.runtime.repository.root))
        directory.setObjectName("pageDescription")
        directory.setWordWrap(True)
        directory.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        directory_text.addWidget(directory)
        directory_layout.addLayout(directory_text, 1)
        open_directory = QPushButton("打开目录")
        open_directory.setProperty("buttonSize", "compact")
        open_directory.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.runtime.repository.root)))
        )
        directory_layout.addWidget(open_directory)
        section("directory", directory_row)
        from lab_control.ui.pages.agent_collaboration import AgentCollaborationPanel

        self.agent_panel = AgentCollaborationPanel(self.runtime, settings_body)
        section("agent", self.agent_panel)
        from lab_control.ui.pages.environment_repair import EnvironmentRepairPanel

        self.environment_panel = EnvironmentRepairPanel(self.runtime, settings_body)
        section("environment", self.environment_panel)
        settings.addStretch(1)
        self.stack.addWidget(self.settings_page)
        self.setCentralWidget(root)
        self.navigate("首页", "", "")

    def toggle_sidebar(self) -> None:
        self._manual_sidebar_collapsed = not self._manual_sidebar_collapsed
        self.update_sidebar()

    def update_sidebar(self) -> None:
        collapsed = self._manual_sidebar_collapsed
        self.sidebar.setFixedWidth(68 if collapsed else SIDEBAR_WIDTH)
        if layout := self.sidebar.layout():
            layout.setContentsMargins(10, 16, 10, 12)
        self.brand.setVisible(not collapsed)
        self.brand_subline.setVisible(not collapsed)
        self.sidebar_section.setVisible(not collapsed)
        self.runtime_badge.setVisible(not collapsed)
        self.chrome.apply_collapsed(collapsed)
        for label, button in self.nav_buttons.items():
            assert isinstance(button, NavButton)
            button.set_collapsed(collapsed)
            button.setToolTip(label)

    def fit_to_screen(self, available: QRect | None = None) -> None:
        """Use Qt logical screen coordinates, including work area and native window decoration."""
        if available is None:
            available = self.screen().availableGeometry()
        border = self.frameGeometry().size() - self.size()
        width = max(1, available.width() - max(0, border.width()))
        height = max(1, available.height() - max(0, border.height()))
        self.setMinimumSize(min(1280, width), min(760, height))
        if not self.isMaximized() and not self.isFullScreen():
            self.resize(min(self.width(), width), min(self.height(), height))
            frame = self.frameGeometry()
            x = max(available.x(), min(frame.x(), available.right() - frame.width() + 1))
            y = max(available.y(), min(frame.y(), available.bottom() - frame.height() + 1))
            self.move(self.pos().x() + x - frame.x(), self.pos().y() + y - frame.y())
        self.update_sidebar()

    def watch_screen(self, screen: QScreen) -> None:
        if self._watched_screen is not None:
            self._watched_screen.availableGeometryChanged.disconnect(self.fit_to_screen)
        self._watched_screen = screen
        screen.availableGeometryChanged.connect(self.fit_to_screen)
        QTimer.singleShot(0, self.fit_to_screen)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if not self._screen_connected and (handle := self.windowHandle()):
            self._screen_connected = True
            handle.screenChanged.connect(self.watch_screen)
            self.watch_screen(handle.screen())

    def focus_card(self, spec: dict) -> None:
        self._navigation.focus_card(spec)

    def _connected_categories(
        self,
        records: dict[str, dict[str, Any]] | None = None,
        states: dict[str, dict[str, Any]] | None = None,
    ) -> frozenset[str]:
        return self._navigation._connected_categories(records, states)

    def _submenu_categories(
        self,
        module: str,
        records: dict[str, dict[str, Any]] | None = None,
        states: dict[str, dict[str, Any]] | None = None,
    ) -> tuple[str, ...]:
        return self._navigation._submenu_categories(module, records, states)

    def _rebuild_submenu(
        self,
        module: str,
        category: str = "",
        records: dict[str, dict[str, Any]] | None = None,
        states: dict[str, dict[str, Any]] | None = None,
    ) -> str:
        return self._navigation._rebuild_submenu(module, category, records, states)

    def _sync_submenu(self) -> None:
        self._navigation._sync_submenu()

    def navigate(self, module: str, category: str = "", device_id: str = "") -> None:
        self._navigation.navigate(module, category, device_id)

    def show_page(self, *_args, device_id: str = "") -> None:
        self._navigation.show_page(*_args, device_id=device_id)

    def _build_overview(self) -> None:
        self._overview._build_overview()

    def prepare_first_run(self) -> None:
        self._actions.prepare_first_run()

    def scan(self) -> None:
        self._actions.scan()

    def select_category(self, *_args) -> None:
        self._overview.select_category(*_args)

    def open_category(self, category: str) -> None:
        self._overview.open_category(category)

    def show_device_pool(self, trigger: QWidget | None = None) -> None:
        self._overview.show_device_pool(trigger)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if hasattr(self, "nav_buttons"):
            self.update_sidebar()
        if hasattr(self, "device_pool") and not self.device_pool.isHidden():
            self.device_pool.position_in_overview()

    def refresh(self) -> None:
        self.controller.refresh_snapshot()
        self._overview.refresh()

    def selection_changed(self) -> None:
        self._overview.selection_changed()

    def open_details(self, device_id: str) -> None:
        self._overview.open_details(device_id)

    def refresh_details(self) -> None:
        self._overview.refresh_details()

    def action(self, key: str) -> None:
        self._actions.action(key)

    def choose_slot(self, device_id: str) -> None:
        self._actions.choose_slot(device_id)

    def connect_all(self) -> None:
        self._actions.connect_all()

    @staticmethod
    def connection_result_text(progress: dict[str, Any]) -> str:
        items = progress.get("items", [])
        counts = {
            status: sum(item["status"] == status for item in items)
            for status in ("connected", "already_connected", "failed", "skipped", "waiting")
        }
        title = {"cancelled": "连接已停止", "error": "批量连接中断"}.get(progress["state"], "连接完成")
        return (
            f"{title} · 成功 {counts['connected']} · 已连接 {counts['already_connected']}"
            f" · 失败 {counts['failed']} · 跳过 {counts['skipped']}"
            + (f" · 等待恢复 {counts['waiting']}" if counts["waiting"] else "")
        )

    def show_connection_results(self) -> None:
        self._actions.show_connection_results()

    def _completed(self, label: str, result: Any) -> None:
        self._actions._completed(label, result)

    def _failed(self, message: str) -> None:
        self._actions._failed(message)

    def diagnose(self) -> None:
        self._actions.diagnose()

    def show_static_addresses(self) -> None:
        self._dialogs.show_static_addresses()

    def show_manual_serial(self, adapter: str) -> None:
        self._dialogs.connect_manual_serial(adapter)

    def configure_adapter(self, device_id: str) -> None:
        self._dialogs.configure_adapter(device_id)

    def closeEvent(self, event) -> None:
        self._shutdown.closeEvent(event)
