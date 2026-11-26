from __future__ import annotations

from typing import TYPE_CHECKING, Any

from PySide6.QtWidgets import (
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from lab_control.application.catalog import instrument_categories, instrument_category, surface_applicable
from lab_control.application.controls import CATEGORY_LABELS
from lab_control.discovery.presence import LIVE_CONNECTIONS
from lab_control.instruments.registry import DEVICE_DEFINITIONS
from lab_control.ui.pages.data_processing import DataProcessingPage
from lab_control.ui.pages.device_control.coremorrow_e00 import CoreMorrowE00Page
from lab_control.ui.pages.device_control.electronic_load import ElectronicLoadPage
from lab_control.ui.pages.device_control.page import DeviceControlPage
from lab_control.ui.pages.device_control.phase_noise import PhaseNoisePage
from lab_control.ui.pages.device_control.power_supply import PowerSupplyPage
from lab_control.ui.pages.device_control.signal_source import SignalSourcePage
from lab_control.ui.pages.monitoring.acceleration import AccelerationMonitoringPage
from lab_control.ui.pages.monitoring.frequency_counter import FrequencyCounterMonitoringPage
from lab_control.ui.pages.monitoring.optical_power import OpticalPowerMonitoringPage
from lab_control.ui.pages.monitoring.optical_spectrum import OpticalSpectrumMonitoringPage
from lab_control.ui.pages.monitoring.oscilloscope import OscilloscopeMonitoringPage
from lab_control.ui.pages.monitoring.page import MonitoringPage
from lab_control.ui.pages.monitoring.power import PowerMonitoringPage
from lab_control.ui.pages.monitoring.spectrum import SpectrumMonitoringPage

if TYPE_CHECKING:
    from lab_control.ui.real_window import RealWindow


class WorkspaceNavigation:
    """Page selection using the controller's cached workspace."""

    def __init__(self, window: RealWindow) -> None:
        self.window = window
        self.last_devices: dict[str, str] = {}

    def focus_card(self, spec: dict) -> None:
        window = self.window
        page = window.stack.currentWidget()
        if getattr(getattr(page, "selector", None), "device_id", None) != spec.get("device_id"):
            return
        focus = getattr(page, "focus_card", None)
        if callable(focus):
            focus(spec)

    def open_recorded_device(self, device_id: str, channel: int | None, category: str = "") -> None:
        self.navigate("仪器测控", category, device_id)
        page = self.window.stack.currentWidget()
        selector = getattr(page, "selector", None)
        if channel is None or selector is None or selector.device_id != device_id:
            return
        if isinstance(page, (OscilloscopeMonitoringPage, SignalSourcePage)) or getattr(
            selector, "bound_device", None
        ):
            self.focus_card({"device_id": device_id, "channels": [channel]})
            return
        selector.select(device_id, channel)
        if isinstance(page, PowerMonitoringPage) and page.controls and channel in page.controls.panels:
            page.controls.scroller.ensureWidgetVisible(page.controls.panels[channel])
        elif isinstance(page, MonitoringPage) and page.scan_page and page.scan_page.isVisible():
            if card := page.scan_page.cards.get(channel):
                page.scan_page.readout_scroll.ensureWidgetVisible(card)
        elif isinstance(page, OpticalPowerMonitoringPage) and channel in page.cards:
            page.readout_scroll.ensureWidgetVisible(page.cards[channel])
        elif check := getattr(page, "channel_checks", {}).get(channel):
            check.setFocus()

    def _connected_categories(
        self,
        records: dict[str, dict[str, Any]] | None = None,
        states: dict[str, dict[str, Any]] | None = None,
    ) -> frozenset[str]:
        window = self.window
        if records is None:
            records = window.controller.records
        if states is None:
            states = window.controller.states
        return frozenset(
            role
            for device_id, record in records.items()
            if states.get(device_id, {}).get("connectionState") in LIVE_CONNECTIONS
            for role in instrument_categories(record)
        )

    def _submenu_categories(
        self,
        module: str,
        records: dict[str, dict[str, Any]] | None = None,
        states: dict[str, dict[str, Any]] | None = None,
    ) -> tuple[str, ...]:
        window = self.window
        if module == "数据处理":
            return ("single_device", "batch")
        if module == "仪器测控":
            control = self._submenu_categories("设备管理", records, states)
            monitor = self._submenu_categories("实时监测", records, states)
            return tuple(c for c in CATEGORY_LABELS if c in control or c in monitor)
        if module not in {"设备管理", "实时监测"}:
            return ()
        monitoring_only = {
            "accelerometer",
            "spectrum_analyzer",
            "oscilloscope",
            "optical_power_meter",
            "optical_spectrometer",
            "frequency_counter",
        }
        if records is None:
            records = window.controller.records
        if states is None:
            states = window.controller.states
        connected = tuple(
            record
            for device_id, record in records.items()
            if states.get(device_id, {}).get("connectionState") in LIVE_CONNECTIONS
        )
        available = {role for record in connected for role in instrument_categories(record)}
        return tuple(
            value
            for value in CATEGORY_LABELS
            if value in available
            and not (module == "设备管理" and value in monitoring_only)
            and any(
                surface_applicable(record, "management" if module == "设备管理" else "monitoring")
                for record in connected
                if value in instrument_categories(record)
            )
        )

    def _rebuild_submenu(
        self,
        module: str,
        category: str = "",
        records: dict[str, dict[str, Any]] | None = None,
        states: dict[str, dict[str, Any]] | None = None,
    ) -> str:
        window = self.window
        categories = window._submenu_categories(module, records, states)
        selected_category = category if category in categories else ""
        window.submenu.blockSignals(True)
        while window.submenu.count():
            window.submenu.removeTab(0)
        if module in {"首页", "仪器测控"}:
            window.submenu.addTab("总览")
            window.submenu.setTabData(0, "")
            selected_index = 0
            for value in categories:
                index = window.submenu.addTab(CATEGORY_LABELS[value])
                window.submenu.setTabData(index, value)
                if value == selected_category:
                    selected_index = index
            window.submenu.setCurrentIndex(selected_index)
        elif module == "数据处理":
            for label, value in (("单设备采集", "single_device"), ("批量采集", "batch")):
                index = window.submenu.addTab(label)
                window.submenu.setTabData(index, value)
            window.submenu.setCurrentIndex(1 if selected_category == "batch" else 0)
        else:
            window.submenu.addTab("软件设置" if module == "系统设置" else module)
            window.submenu.setTabData(0, "")
        window.submenu.blockSignals(False)
        window._submenu_signature = (module, categories)
        return selected_category

    def _sync_submenu(self) -> None:
        window = self.window
        if window.current_module != "仪器测控":
            return
        categories = window._submenu_categories(window.current_module, window.records, window.states)
        current_category = window.submenu.tabData(window.submenu.currentIndex()) or ""
        if (window.current_module, categories) != window._submenu_signature:
            if current_category in categories:
                window._rebuild_submenu(window.current_module, current_category)
            else:
                window.navigate(window.current_module)

    def _device_page(self, category: str, device_id: str) -> tuple[str, str]:
        controller = self.window.controller
        if not device_id:
            keys = [r["id"] for r in controller.connected_records(category)]
            remembered = self.last_devices.get(category)
            device_id = remembered if remembered is not None and remembered in keys else next(iter(keys), "")
        record = controller.records.get(device_id)
        control = category == "signal_generator" or (record and not surface_applicable(record, "monitoring"))
        return ("设备管理" if control else "实时监测", device_id)

    def navigate(self, module: str, category: str = "", device_id: str = "") -> None:
        window = self.window
        current = window.stack.currentWidget()
        selector = getattr(current, "selector", None)
        if selector and selector.device_id:
            self.last_devices[selector.category] = selector.device_id
        if device_id and module in {"设备管理", "实时监测", "仪器测控"}:
            record = window.controller.records.get(device_id)
            if record is None:
                self.navigate("首页")
                window.controller.message.emit("设备已不在设备池中")
                return
            if category not in instrument_categories(record):
                category = instrument_category(record)
            if window.controller.states.get(device_id, {}).get("connectionState") not in LIVE_CONNECTIONS:
                self.navigate("首页")
                window.open_details(device_id)
                return
        if module == "设备管理":
            module = "仪器测控" if category else "首页"
        elif module == "实时监测":
            module = "仪器测控"
        if module not in window.nav_buttons:
            return
        if module == "仪器测控" and category:
            surface, device_id = self._device_page(category, device_id)
            cached = window.page_map.get((surface, category))
            selector = getattr(cached, "selector", None)
            if selector and device_id != selector.device_id and not selector.isEnabled():
                window.controller.message.emit("设备正在操作，请稍后切换")
                return
        window.device_pool.hide()
        window.current_module = module
        for name, button in window.nav_buttons.items():
            button.setChecked(name == module)
        window.section_title.setText(module)
        window._rebuild_submenu(module, category)
        self.show_page(device_id=device_id)

    def watchlists_page(self):
        from lab_control.ui.pages.watchlists import WatchlistsPage

        window = self.window
        key = ("监视列表", "")
        page = window.page_map.get(key)
        if page is None:
            page = WatchlistsPage(window.watch_hub, window)
            window.page_map[key] = page
            window.stack.addWidget(page)
        return page

    def show_page(self, *_args, device_id: str = "") -> None:
        window = self.window
        window.device_pool.hide()
        module, category = window.current_module, window.submenu.tabData(window.submenu.currentIndex()) or ""
        window.subnav.setVisible(module == "数据处理")
        window.chrome.set_crumbs(
            module if module == "仪器测控" and category else "",
            CATEGORY_LABELS.get(category, module) if module == "仪器测控" and category else module,
        )
        if module == "仪器测控" and category:
            module, device_id = self._device_page(category, device_id)
            record = window.controller.records.get(device_id, {})
            definition = DEVICE_DEFINITIONS.get(record.get("adapterId", ""))
            if definition and definition.role_page_factory and category in definition.role_categories:
                key = (device_id, category)
                page = window.page_map.get(key)
                if page is None:
                    page = definition.role_page_factory(window.controller, device_id, category)
                    window.page_map[key] = page
                    window.stack.addWidget(page)
                window.stack.setCurrentWidget(page)
                window.chrome.sync_selection()
                return
        if module == "首页":
            page = window.overview_page
        elif module == "仪器测控" and not category:
            page = window.dashboard
        elif module == "监视列表":
            page = self.watchlists_page()
        elif module == "系统设置":
            page = window.settings_page
        elif module == "操作日志":
            from lab_control.ui.pages.operation_log import OperationLogPage

            page = window.page_map.get((module, ""))
            if page is None:

                def resolve(device_id: str) -> str:
                    record = window.controller.records.get(device_id)
                    if not record:
                        return ""
                    identity = record.get("identity") or {}
                    model = identity.get("model") or record.get("adapterId", "")
                    name = record.get("displayName") or ""
                    return " · ".join(v for v in (model, name) if v)

                page = OperationLogPage(window.runtime.repository, window, resolve_device=resolve)
                window.page_map[(module, "")] = page
                window.stack.addWidget(page)
        elif module == "数据处理":
            page = window.page_map.get((module, ""))
            if page is None:
                page = DataProcessingPage(window.controller)
                page.open_device_requested.connect(self.open_recorded_device)
                window.page_map[(module, "")] = page
                window.stack.addWidget(page)
            assert isinstance(page, DataProcessingPage)
            page.set_scope(category)
        else:
            key = (module, category)
            page = window.page_map.get(key)
            if page is None:
                if module == "设备管理":
                    page = (
                        SignalSourcePage(window.controller)
                        if category == "signal_generator"
                        else CoreMorrowE00Page(window.controller)
                        if category == "power_supply"
                        else PhaseNoisePage(window.controller)
                        if category == "phase_noise_analyzer"
                        else ElectronicLoadPage(window.controller)
                        if category == "electronic_load"
                        else DeviceControlPage(window.controller, category)
                    )
                elif module == "实时监测":
                    page = (
                        SpectrumMonitoringPage(window.controller)
                        if category == "spectrum_analyzer"
                        else OscilloscopeMonitoringPage(window.controller)
                        if category == "oscilloscope"
                        else PowerMonitoringPage(window.controller)
                        if category == "power_supply"
                        else OpticalPowerMonitoringPage(window.controller)
                        if category == "optical_power_meter"
                        else AccelerationMonitoringPage(window.controller)
                        if category == "accelerometer"
                        else OpticalSpectrumMonitoringPage(window.controller)
                        if category == "optical_spectrometer"
                        else FrequencyCounterMonitoringPage(window.controller)
                        if category == "frequency_counter"
                        else MonitoringPage(window.controller, category)
                    )
                else:
                    page = QWidget()
                    layout = QVBoxLayout(page)
                    title = QLabel(module)
                    title.setObjectName("pageTitle")
                    layout.addWidget(title)
                    message = {
                        "操作日志": "设备操作、监测与异常按日保存在软件运行目录的 logs 文件夹。",
                    }[module]
                    layout.addWidget(QLabel(message))
                    button = QPushButton("进入仪器测控")
                    button.clicked.connect(lambda: window.navigate("实时监测"))
                    layout.addWidget(button)
                    layout.addStretch()
                window.page_map[key] = page
                window.stack.addWidget(page)
        assert page is not None
        if device_id and isinstance(
            page,
            (
                DeviceControlPage,
                ElectronicLoadPage,
                MonitoringPage,
                SpectrumMonitoringPage,
                OscilloscopeMonitoringPage,
                PowerMonitoringPage,
                OpticalPowerMonitoringPage,
                OpticalSpectrumMonitoringPage,
                PhaseNoisePage,
                FrequencyCounterMonitoringPage,
                SignalSourcePage,
                PowerSupplyPage,
                CoreMorrowE00Page,
            ),
        ):
            # Resolve a requested device before showEvent can start readback for the page.
            page.selector.select(device_id)
        window.stack.setCurrentWidget(page)
        window.chrome.sync_selection()
