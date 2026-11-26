"""Exercise the production page routes against the full simulated instrument inventory."""

from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QAbstractButton, QBoxLayout, QPushButton

from lab_control.application.catalog import instrument_categories, instrument_category, surface_applicable
from lab_control.application.config import config_from_record
from lab_control.application.controls import data_sources
from lab_control.demo.workspace import create_demo_runtime
from lab_control.instruments.registry import DEVICE_DEFINITIONS
from lab_control.ui.kit import DeviceChips, PageHeader
from lab_control.ui.pages.monitoring.timing import MonitoringInterval
from lab_control.ui.real_window import RealWindow
from lab_control.ui.widgets.device_selector import DeviceSelector


def test_registered_devices_share_header_layout_and_automatic_overview(qtbot, qapp, tmp_path):
    runtime = create_demo_runtime(tmp_path, all_models=True)
    window = RealWindow(runtime)
    qtbot.addWidget(window)
    window.show()
    try:
        # The lower minimum exercises the page header independently of the OS screen.
        window.setMinimumWidth(600)
        records = window.controller.connected_records()
        assert {r["adapterId"] for r in records} == set(DEVICE_DEFINITIONS)
        for width in (1440, 920):
            window.resize(width, 900)
            for record in records:
                for role in instrument_categories(record):
                    window.navigate("仪器测控", role, record["id"])
                    qapp.processEvents()
                    page = window.stack.currentWidget()
                    selectors = [w for w in page.findChildren(DeviceSelector) if w.isVisible()]
                    assert len(selectors) == 1, (record["adapterId"], role)
                    selector = selectors[0]
                    assert selector.combo.currentData() == record["id"] and selector.combo.isVisible(), (
                        width,
                        role,
                        record["adapterId"],
                        window.controller.records[selector.device_id]["adapterId"],
                    )
                    # Some models briefly lock switching while reading their initial setup.
                    qtbot.waitUntil(selector.isEnabled, timeout=10000)
                    assert not selector.findChildren(DeviceChips)
                    assert not any(
                        "固定到仪器测控总览" in b.toolTip() for b in page.findChildren(QAbstractButton)
                    )
                    intervals = [w for w in page.findChildren(MonitoringInterval) if w.isVisible()]
                    starts = [
                        b
                        for b in page.findChildren(QAbstractButton)
                        if b.isVisible() and b.text() == "开始监测"
                    ]
                    if not intervals:
                        assert not starts, (record["adapterId"], role)
                        continue
                    (field,) = intervals
                    (start,) = starts
                    left = field.mapTo(page, QPoint(0, field.height() // 2))
                    right = start.mapTo(page, QPoint(0, start.height() // 2))
                    assert abs(left.y() - right.y()) <= 2, (role, width)
                    assert 0 <= right.x() - left.x() - field.width() <= 12, (role, width)
                    assert field.minimum() == 0 and field.maximum() == 60
        # Exercise a selection through the actual device-card menu.
        window.navigate("仪器测控", "spectrum_analyzer")
        selector = window.stack.currentWidget().selector
        fph = next(r["id"] for r in records if r["adapterId"] == "rs.fph")
        dsa = next(r["id"] for r in records if r["adapterId"] == "rigol.dsa815")
        for device_id in (dsa, fph):
            qtbot.waitUntil(selector.isEnabled)
            combo = selector.combo
            combo.showPopup()
            qtbot.waitUntil(combo.view().isVisible)
            row = combo.model().index(combo.findData(device_id), 0)
            qtbot.mouseClick(
                combo.view().viewport(), Qt.MouseButton.LeftButton, pos=combo.view().visualRect(row).center()
            )
            assert window.stack.currentWidget().device_id == device_id
        window.navigate("仪器测控")
        dashboard = window.dashboard
        eligible = {
            r["id"]
            for r in records
            if instrument_category(r) != "signal_generator"
            and surface_applicable(r, "monitoring")
            and data_sources(config_from_record(r))
        }
        assert set(dashboard.cards) == eligible
        devices = {r["id"] for r in records if r["adapterId"] in {"rs.fph", "rigol.dsa815"}}
        assert len(devices) == 2 and devices <= dashboard.cards.keys()
        for key, card in dashboard.cards.items():
            card.enabled.setChecked(key in devices)
        for key in devices:
            dashboard.cards[key].interval.setValue(0.01)
        dashboard.sample_button.click()
        qtbot.waitUntil(
            lambda: all(runtime.monitoring.snapshots().get(k, {}).get("recording") for k in devices),
            timeout=10000,
        )
        dashboard.sample_button.click()
        qtbot.waitUntil(lambda: all(not runtime.monitoring.snapshots()[k]["running"] for k in devices))
    finally:
        window.timer.stop()
        window.controller.close()
        runtime.close()
        window.closing = True
        window.bridge.worker.shutdown()


def test_header_wraps_interval_and_start_onto_same_row(qtbot, qapp):
    header = PageHeader()
    qtbot.addWidget(header)
    device, interval, start = (QPushButton(t) for t in ("设备", "采集间隔", "开始监测"))
    for widget, width in ((device, 240), (interval, 120), (start, 100)):
        widget.setFixedWidth(width)
    header.add_left(device)
    header.add_right(interval)
    header.add_right(start)
    header.resize(640, 100)
    header.show()
    qapp.processEvents()
    assert header.row.direction() == QBoxLayout.Direction.LeftToRight
    header.resize(380, 100)
    qapp.processEvents()
    assert header.row.direction() == QBoxLayout.Direction.TopToBottom
    assert interval.y() == start.y() and interval.y() >= device.y() + device.height()
    assert 0 <= start.x() - interval.x() - interval.width() <= 12
