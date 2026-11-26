import threading
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QMessageBox

from lab_control.app import load_stylesheet, load_ui_font
from lab_control.application.config import ConnectionConfig
from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.instruments import DeviceError
from lab_control.ui.pages.monitoring.power import PowerMonitoringPage
from lab_control.ui.pages.monitoring.timing import MonitoringInterval, monitoring_options
from lab_control.ui.real_window import RealWindow
from lab_control.ui.widgets.popup_surface import WorkspaceMenu


@pytest.fixture
def power(qapp, qtbot, tmp_path):
    qapp.setStyle("Fusion")
    qapp.setFont(QFont(load_ui_font(), 10))
    qapp.setStyleSheet(load_stylesheet())
    runtime = ApplicationRuntime(root=tmp_path, hardware=False)
    key = runtime.add(ConnectionConfig("mock://power-workspace", "gwinstek.gpd4303s", "mock"))
    runtime.connect(key).result(5)
    window = RealWindow(runtime)
    qtbot.addWidget(window)
    window.show()
    window.navigate("设备管理", "power_supply", key)
    page = window.stack.currentWidget()
    try:
        assert isinstance(page, PowerMonitoringPage)
        qtbot.waitUntil(lambda: all(page.controls.current_settings(ch) for ch in (1, 2, 3, 4)), timeout=8000)
        yield window, page, runtime, key, runtime.sessions.sessions[key].adapter
    finally:
        window.timer.stop()
        window.controller.close()
        runtime.close()
        window.closing = True
        window.bridge.worker.shutdown()


@pytest.mark.parametrize(
    "old", [{"cadence": "fastest", "interval": 0.5}, {"cadence": "interval", "interval": 60}]
)
def test_power_startup_restores_numeric_and_legacy_fastest_intervals(tmp_path, qtbot, old):
    with ApplicationRuntime(root=tmp_path, hardware=False) as runtime:
        key = runtime.add(ConnectionConfig("mock://power-start", "gwinstek.gpd4303s", "mock"))
        pref = f"monitor-timing/general/{key}"
        runtime.repository.save_workspace(pref, old)
    expected = 0 if old["cadence"] == "fastest" else old["interval"]
    for _ in range(2):
        with ApplicationRuntime(root=tmp_path, hardware=False) as runtime:
            controller = SimpleNamespace(
                runtime=runtime, records={r["id"]: r for r in runtime.inventory.all()}
            )
            widget = MonitoringInterval(controller)
            qtbot.addWidget(widget)
            widget.bind(key, "general")
            assert widget.options() == dict(
                interval=expected, cadence="interval", record_interval=None, csv_interval=5
            )
            assert widget.minimum() == 0 and widget.maximum() == 60
            for seconds in (0.01, 1.27, 60, 1):
                widget.setValue(seconds)
                assert monitoring_options(controller, key, "general")["interval"] == seconds
            widget.setValue(10)
            widget.bind("", "general")
            widget.bind(key, "general")
            assert widget.options()["interval"] == 10
            assert monitoring_options(controller, key, "optical")["interval"] == 1
            assert not runtime.monitoring.tasks and not runtime.sessions.sessions
            expected = 10


def test_measured_voltage_and_current_columns_do_not_move_with_text_length(power, qtbot):
    window, page, *_ = power
    labels = page.controls.panels[1].measurements
    window.resize(1100, 760)
    qtbot.wait(20)
    positions = None
    for voltage, current in (("1", "2"), ("-123456.789012", "1e-12"), ("—", "123456789.123456")):
        labels["voltage"].setText(f"实测 {voltage} V")
        labels["current"].setText(f"实测 {current} A")
        qtbot.wait(15)
        geometry = {key: label.geometry().getRect() for key, label in labels.items()}
        if positions is None:
            positions = geometry
        assert geometry == positions


def test_power_chart_menus_use_clicked_plot_and_opening_frame(power, qtbot, monkeypatch):
    _, page, runtime, key, adapter = power
    voltage, current = (page.plot.plots[q] for q in ("voltage", "current"))
    for canvas, position in ((voltage, QPoint(2, 2)), (current, current.viewport().rect().center())):
        qtbot.mouseClick(canvas.viewport(), Qt.MouseButton.RightButton, pos=position)
        menu = next(m for m in canvas.findChildren(WorkspaceMenu) if m.isVisible())
        actions = [a for a in menu.actions() if not a.isSeparator()]
        assert [a.text() for a in actions] == ["恢复视图", "保存当前帧数据", "导出 PNG"]
        assert all(not a.isEnabled() and a.menu() is None for a in actions)
        qtbot.keyClick(menu, Qt.Key.Key_Escape)
        assert not menu.isVisible()

    saved, images = [], []
    monkeypatch.setattr(page.controller, "save_displayed_frame", lambda *args: saved.append(args))
    page.start()
    qtbot.waitUntil(lambda: page.plot.displayed_frame is not None and page.save_button.isEnabled())
    opening_frame = page.plot.displayed_frame
    menus = []
    for canvas in (voltage, current):
        monkeypatch.setattr(canvas, "save_image", images.append)
        menus.append(canvas.build_menu())
    qtbot.waitUntil(lambda: page.plot.displayed_frame.sequence > opening_frame.sequence, timeout=5000)
    runtime.monitoring.stop(key).result(5)
    page.controller.poll()
    page.refresh()
    monkeypatch.setattr(adapter, "acquire", lambda *_: pytest.fail("a chart action read the instrument"))
    monkeypatch.setattr(adapter, "apply", lambda *_: pytest.fail("a chart action wrote to the instrument"))
    for chart, menu in zip((voltage, current), menus, strict=True):
        fit, save, png = (a for a in menu.actions() if not a.isSeparator())
        assert all(a.isEnabled() for a in (fit, save, png))
        chart.setXRange(100, 105, padding=0)
        for canvas in (voltage, current):
            canvas.setYRange(100, 200, padding=0)
        fit.trigger()
        assert all(canvas.viewRange()[0] == pytest.approx((0, 600)) for canvas in (voltage, current))
        assert all(canvas.viewRange()[1][0] < 100 for canvas in (voltage, current))
        save.trigger()
        png.trigger()
        assert saved[-1] == (page, key, opening_frame, page.mode)
        assert images[-1].ylabel.startswith(chart.getAxis("left").labelText)
        expected = [
            m
            for m in opening_frame.batch.measurements
            if m.quantity == ("voltage" if chart is voltage else "current")
        ]
        assert len(images[-1].curves) == len(expected)
        for m in expected:
            values = next(y for name, x, y in images[-1].curves if name == f"CH {m.channel}")
            np.testing.assert_array_equal(values[-1:], m.values)
            assert len(values) < len(page.plot.history[m.channel, m.quantity])
    assert {m.quantity for m in saved[-1][2].batch.measurements} == {"voltage", "current"}

    paused_frame = page.plot.displayed_frame
    qtbot.mouseClick(current.viewport(), Qt.MouseButton.RightButton, pos=current.viewport().rect().center())
    menu = next(m for m in current.findChildren(WorkspaceMenu) if m.isVisible())
    menu.setActiveAction(menu.actions()[0])
    qtbot.keyClick(menu, Qt.Key.Key_Down)
    assert menu.activeAction().text() == "保存当前帧数据"
    qtbot.keyClick(menu, Qt.Key.Key_Return)
    assert not menu.isVisible() and saved[-1][2] is paused_frame


def test_navigation_one_read_feeds_both_trends_and_no_parameter_polling(power, qtbot, monkeypatch):
    window, page, runtime, key, adapter = power
    assert window.current_module == "仪器测控"
    assert page.channels() == (1, 2, 3, 4)
    assert page.controls.output.operation == "all_outputs"
    assert all(panel.output is None for panel in page.controls.panels.values())
    assert not runtime.monitoring.tasks
    reads, settings = [], []
    acquire, read_settings = adapter.acquire, adapter.read_settings
    monkeypatch.setattr(adapter, "acquire", lambda request: (reads.append(request), acquire(request))[1])
    monkeypatch.setattr(
        adapter, "read_settings", lambda *args: (settings.append(args), read_settings(*args))[1]
    )
    page.start()
    qtbot.waitUntil(lambda: page.plot.displayed_frame is not None)
    # A quiet read already queued before start may finish first; stable monitoring
    # must never schedule another independent parameter query.
    settings.clear()
    qtbot.waitUntil(lambda: len(reads) >= 2 and page.plot.displayed_frame is not None, timeout=5000)
    assert len(runtime.monitoring.tasks[key].requests) == 1
    assert len(page.plot.displayed_frame.batch.measurements) == 8
    assert len(page.plot.curves) == 8
    assert all(request.channels == (1, 2, 3, 4) and request.points == 1 for request in reads)
    assert not settings
    assert page.actual.toolTip() == ""
    assert page.timing.isVisible() and page.actual.isVisible()
    window.dashboard.refresh()
    assert set(window.dashboard.cards[key].spec["sources"]) == {"voltage", "current"}
    window.dashboard.start()
    assert len(runtime.monitoring.tasks[key].requests) == 1


def test_control_during_monitoring_preserves_other_draft_and_cached_output(power, qtbot, monkeypatch):
    _, page, runtime, key, adapter = power
    page.start()
    qtbot.waitUntil(lambda: page.plot.displayed_frame is not None)
    frames = runtime.sessions.sessions[key].frames
    for frame_key, frame in tuple(frames.items()):
        frames[frame_key] = replace(frame, received_at=frame.received_at - 10000)
    page.controller.poll()
    page.controls.output.refresh()
    assert page.controls.output.button.isEnabled()
    before = frames[(1, "all_outputs")].reading.timestamp
    page.controls.output.refresh()
    assert frames[(1, "all_outputs")].reading.timestamp == before
    changes = []
    apply = adapter.apply
    monkeypatch.setattr(adapter, "apply", lambda cmd: (changes.append(cmd), apply(cmd))[1])
    for ch, value in ((1, "2.5"), (2, "3.5")):
        field = page.controls.panels[ch].editors["voltage"].editors["value"].input
        field.setFocus()
        field.selectAll()
        qtbot.keyClicks(field, value)
    page.selector.combo.setFocus()
    first = page.controls.panels[1]
    qtbot.waitUntil(first.apply_button.isEnabled)
    first.apply_button.click()
    qtbot.waitUntil(lambda: not first.running and not page.controls.running, timeout=5000)
    assert [(cmd.channel, cmd.operation) for cmd in changes] == [(1, "voltage")]
    assert page.controls.panels[2].editors["voltage"].dirty
    assert runtime.monitoring.snapshots()[key]["running"]
    assert all(page.controls.current_settings(ch) for ch in (1, 2, 3, 4))
    assert not page.controls.panels[1].editors["voltage"].dirty


def test_channel_change_drains_old_read_and_empty_selection_stops(power, qtbot, monkeypatch):
    _, page, runtime, key, adapter = power
    entered, release = threading.Event(), threading.Event()
    acquire = adapter.acquire
    requests = []

    def slow(request):
        requests.append(request)
        entered.set()
        assert release.wait(5)
        return acquire(request)

    monkeypatch.setattr(adapter, "acquire", slow)
    page.start()
    qtbot.waitUntil(entered.is_set)
    try:
        page.channel_checks[4].setChecked(False)
        assert page.transition
        assert len(requests) == 1
    finally:
        release.set()
    qtbot.waitUntil(lambda: len(requests) >= 2 and not page.transition)
    assert requests[-1].channels == (1, 2, 3)
    for ch in (1, 2, 3):
        page.channel_checks[ch].setChecked(False)
    qtbot.waitUntil(lambda: not page.transition)
    assert not runtime.monitoring.snapshots()[key]["running"]
    assert not page.plot.displayed_frame


def test_record_every_frame_locks_controls_and_display_save_uses_old_frame(
    power, qtbot, monkeypatch, frame_export_directory
):
    _, page, runtime, key, adapter = power
    page.start()
    qtbot.waitUntil(lambda: page.plot.displayed_frame is not None)
    page.record()
    qtbot.waitUntil(
        lambda: runtime.monitoring.snapshots()[key].get("recording_state", {}).get("committed", 0) >= 2,
        timeout=6000,
    )
    page.controller.poll()
    page.refresh()
    assert all(not check.isEnabled() for check in page.channel_checks.values())
    assert not page.controls.output.button.isEnabled()
    task = runtime.monitoring.tasks[key]
    assert task.record_interval is None and task.csv_interval == 5
    page.record()
    qtbot.waitUntil(lambda: task.recorder.binary_done, timeout=6000)
    assert task.recorder.snapshot()["received"] == task.recorder.snapshot()["committed"]
    runtime.monitoring.stop(key).result(5)
    displayed = page.plot.displayed_frame
    seen = []
    monkeypatch.setattr(
        adapter, "acquire", lambda _r: pytest.fail("Saving a displayed frame queried the instrument")
    )
    monkeypatch.setattr(
        runtime.repository,
        "export",
        lambda batch, mode, **kw: (seen.append(batch), runtime.repository.root / "snapshot.json")[1],
    )
    page.save()
    qtbot.waitUntil(lambda: bool(seen))
    assert seen[0].timestamp == displayed.batch.timestamp
    assert len(seen[0].measurements) == 8
    assert seen[0].timing == displayed.batch.timing


@pytest.mark.parametrize("failure", ["timeout", "incomplete"])
def test_bad_explicit_read_stops_monitor_without_retry_or_draft_loss(power, qtbot, monkeypatch, failure):
    _, page, runtime, key, adapter = power
    page.start()
    qtbot.waitUntil(lambda: page.plot.displayed_frame is not None)
    # Stop resumes unrelated quiet synchronization; isolate the explicit read
    # so its no-replay assertion does not count that normal background work.
    runtime.monitoring.enable_settings_sync(False)
    calls = []

    def broken(*args):
        calls.append(args)
        if failure == "incomplete":
            return ()
        raise DeviceError("read timeout", "timeout")

    monkeypatch.setattr(adapter, "read_settings", broken)
    page.controls.read_parameters()
    qtbot.waitUntil(lambda: not page.controls.running)
    assert len(calls) == 1
    assert not runtime.monitoring.snapshots()[key]["running"]
    assert ("timeout" if failure == "timeout" else "不完整") in page.controls.status.text()


def test_stop_during_channel_drain_does_not_restart(power, qtbot, monkeypatch):
    _, page, runtime, key, adapter = power
    entered, release = threading.Event(), threading.Event()
    acquire = adapter.acquire
    requests = []

    def slow(request):
        requests.append(request)
        entered.set()
        assert release.wait(5)
        return acquire(request)

    monkeypatch.setattr(adapter, "acquire", slow)
    page.start()
    qtbot.waitUntil(entered.is_set)
    try:
        page.channel_checks[4].setChecked(False)
        assert page.transition and page.start_button.isEnabled()
        page.start_button.click()
    finally:
        release.set()
    qtbot.waitUntil(lambda: not page.transition)
    assert len(requests) == 1
    assert not runtime.monitoring.snapshots()[key]["running"]


def test_interval_in_run_and_hidden_plot_reconnect_session(power, qtbot, monkeypatch):
    window, page, runtime, key, _ = power
    page.start()
    qtbot.waitUntil(lambda: page.plot.displayed_frame is not None)
    old = page.plot.displayed_frame
    page.timing.setValue(5)
    assert runtime.monitoring.snapshots()[key]["interval"] == 5
    window.navigate("实时监测")
    assert not page.isVisible()
    original = page.plot.show_frame
    monkeypatch.setattr(page.plot, "show_frame", lambda *_: pytest.fail("Hidden page drew a frame"))
    page.refresh_frame(key)
    monkeypatch.setattr(page.plot, "show_frame", original)
    runtime.monitoring.stop(key).result(5)
    runtime.sessions.disconnect(key, drain_reads=True)
    window.controller.poll()
    runtime.connect(key).result(5)
    window.controller.poll()
    window.navigate("实时监测", "power_supply", key)
    assert page.timing.options()["interval"] == 5
    assert page.plot.displayed_frame is None
    assert not page.plot.history
    assert page.session_id != old.session_id
    assert not runtime.monitoring.snapshots().get(key, {}).get("running")


def test_channel_output_capability_does_not_invent_master(power, qtbot):
    window, page, runtime, _, _ = power
    key = runtime.add(ConnectionConfig("mock://channel-output", "itech.it6322", "mock"))
    runtime.connect(key).result(5)
    window.controller.poll()
    page.selector.select(key)
    qtbot.waitUntil(lambda: page.device_id == key)
    assert page.controls.output is None
    assert all(panel.output is not None for panel in page.controls.panels.values())
    assert page.channels() == runtime.sessions.capability_snapshot(key).channels[1].channels


def test_global_output_confirmation_and_readback_continue_monitoring(power, qtbot, monkeypatch):
    _, page, runtime, key, adapter = power
    page.start()
    qtbot.waitUntil(lambda: page.plot.displayed_frame is not None)
    output = page.controls.output
    writes = []
    apply = adapter.apply

    def controlled(command):
        assert key in page.controller.group_devices
        writes.append(command)
        return apply(command)

    read = adapter.read_settings

    def readback(*args):
        assert key in page.controller.group_devices
        return read(*args)

    monkeypatch.setattr(adapter, "apply", controlled)
    monkeypatch.setattr(adapter, "read_settings", readback)
    monkeypatch.setattr(QMessageBox, "question", lambda *a: QMessageBox.StandardButton.No)
    output.button.click()
    assert not writes
    monkeypatch.setattr(QMessageBox, "question", lambda *a: QMessageBox.StandardButton.Yes)
    output.button.click()
    qtbot.waitUntil(lambda: not output.busy and not page.controls.running)
    qtbot.waitUntil(lambda: output.value is True)
    assert len(writes) == 1 and writes[0].operation == "all_outputs"
    assert key not in page.controller.group_devices
    assert runtime.monitoring.snapshots()[key]["running"]
    assert all(page.controls.current_settings(ch) for ch in page.controls.panels)


def test_header_interval_is_visible_without_starting_or_writing(power, qtbot, monkeypatch):
    _, page, runtime, key, adapter = power
    monkeypatch.setattr(adapter, "apply", lambda *_: pytest.fail("Interval tool wrote to instrument"))
    monkeypatch.setattr(adapter, "acquire", lambda *_: pytest.fail("Interval tool started acquisition"))
    assert page.timing.isVisible() and not hasattr(page, "timing_action")
    assert page.actual.isVisible() and page.actual.toolTip() == ""
    assert page.timing.isVisible() and page.actual.isVisible()
    page.timing.setValue(5)
    assert page.timing.isVisible() and page.actual.isVisible()
    assert monitoring_options(page.controller, key, page.mode)["interval"] == 5
    assert not runtime.monitoring.snapshots().get(key, {}).get("running")
