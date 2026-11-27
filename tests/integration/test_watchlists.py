from dataclasses import replace

import pytest
from PySide6.QtCore import QModelIndex, Qt
from PySide6.QtWidgets import QInputDialog, QMessageBox

from lab_control.application.runtime import ApplicationRuntime
from lab_control.application.watchlists import Preset, Presets, WatchRow, bounds_status
from lab_control.demo.instrument import SimulatedInstrument
from lab_control.domain.connections import ConnectionConfig
from lab_control.domain.instruments import AcquisitionRequest, DeviceError, Measurement
from lab_control.ui.pages.watchlists import WatchlistsPage
from lab_control.ui.real_window import RealWindow


@pytest.fixture
def workspace(tmp_path, qtbot):
    rt = ApplicationRuntime(root=tmp_path, hardware=False)
    instruments = {}

    def make(config):
        inst = SimulatedInstrument(config)
        if config.adapter == "keithley.2701":
            inst.caps = replace(inst.caps, channels=(101, 204))
            inst.capability_details = {
                "acquisition_channel_selection": "scan_list",
                "acquisition_channels": [101, 204],
                "reading_quantity": "temperature",
                "reading_unit": "°C",
            }
            acquire = inst.acquire

            def read(request):
                batch = acquire(request)
                return replace(
                    batch,
                    measurements=tuple(
                        Measurement("temperature", "°C", (22.000001,), ch) for ch in request.channels
                    ),
                )

            inst.acquire = read
        instruments[config.resource] = inst
        return inst

    rt.sessions.factory = make
    meter = rt.add(ConnectionConfig("mock://meter", "keithley.2701", "mock"))
    scope = rt.add(ConnectionConfig("mock://scope", "rigol.dho924", "mock"))
    for d in (meter, scope):
        rt.connect(d).result(5)
    slot = rt.create_slot("multimeter", "试验箱")
    rt.bind_slot(meter, slot, "general")
    window = RealWindow(rt)
    qtbot.addWidget(window)
    window.show()
    window.navigate("监视列表")
    page = window.stack.currentWidget()
    assert isinstance(page, WatchlistsPage)
    try:
        yield window, rt, page, meter, scope
    finally:
        page.shutdown()
        window.watch_hub.close()
        window.timer.stop()
        window.controller.close()
        rt.close()
        window.closing = True
        window.bridge.worker.shutdown()


def test_windows_share_task_order_and_thresholds(workspace, qtbot):
    win, rt, page, meter, scope = workspace
    hub = page.hub
    assert tuple(win.nav_buttons) == ("首页", "仪器测控", "监视列表", "数据处理", "操作日志", "系统设置")
    assert set(win._submenu_categories("仪器测控")) == {"multimeter", "oscilloscope"}
    assert not win._submenu_categories("首页")
    channels, specs = hub.catalog(meter)
    assert channels == (101, 204)
    s = specs[("reading", "", "")]
    rows = tuple(s.row(win.controller.records[meter], ch, {}) for ch in channels)
    first = Preset("机箱", rows)
    second = Preset("副屏", (replace(rows[1], id="extra", upper=22),))
    hub.save([first, second])
    popup = page.open_window(second.id)
    assert page.open_window(second.id) is popup
    qtbot.waitUntil(lambda: all(r.id in hub.values for r in (*rows, *second.rows)), timeout=9000)
    task = rt.monitoring.tasks[meter]
    assert task.watch_owned and len(task.requests) == 1 and task.requests[0].channels == (101, 204)
    assert hub.status["extra"] == "高于上限"
    (alarm,) = win.chrome.watchlist_alarms()
    assert alarm["device_id"] == meter and "204" in alarm["detail"]
    assert alarm["value"] == "22.000001 °C" and alarm["limit"] == "上限 22 °C"
    model = page.table.model
    assert model.setData(model.index(1, 5), "23")
    qtbot.waitUntil(lambda: hub.status[rows[1].id] == "低于下限")
    assert {item["limit"] for item in win.chrome.watchlist_alarms()} == {"下限 23 °C", "上限 22 °C"}
    assert model.setData(model.index(1, 5), "22.000001")
    assert model.setData(model.index(1, 6), "22.000001")
    assert not model.setData(model.index(1, 5), "23")
    qtbot.waitUntil(lambda: hub.status[rows[1].id] == "正常")
    assert model.dropMimeData(
        model.mimeData([model.index(1, 0)]), Qt.DropAction.MoveAction, 0, 0, model.index(-1, -1)
    )
    assert model.rows[0].channel == 204
    assert Presets(rt.repository).get(first.id).rows[0].lower == 22.000001
    win.navigate("首页")
    assert rt.monitoring.tasks[meter] is task and task.running
    popup.close()
    qtbot.waitUntil(lambda: not task.running)
    assert rt.monitoring.stop_revision(meter) == 0
    assert win.chrome.watchlist_alarms() == []


def test_ongoing_scope_and_recording_take_priority(workspace, qtbot, monkeypatch):
    win, rt, page, meter, scope = workspace
    hub = page.hub
    forbidden = []
    monkeypatch.setattr(rt.sessions, "submit_single", lambda *a, **kw: forbidden.append(a))
    req = AcquisitionRequest("waveform", (1,), points=256, confirmed=True)
    rt.monitoring.start(scope, (req,), interval=0.5)
    task = rt.monitoring.tasks[scope]
    rt.monitoring.set_recording(scope, True)
    try:
        spec = hub.catalog(scope)[1][("waveform", "voltage.waveform_statistics", "rms")]
        rows = tuple(spec.row(win.controller.records[scope], ch, {}) for ch in (1, 2))
        preset = Preset("波形", rows)
        hub.save([preset])
        trend = hub.start_recording(preset.id)
        dlg = page.open_window(preset.id)
        qtbot.waitUntil(lambda: rows[0].id in hub.values, timeout=9000)
        qtbot.waitUntil(lambda: trend.count >= 2, timeout=8000)
        hub.stop_recording(preset.id)
        qtbot.waitUntil(trend.done.is_set)
        assert not trend.error
        assert hub.values[rows[0].id][1].unit == "V"
        assert hub.status[rows[1].id] == "等待兼容数据"
        with pytest.raises(DeviceError):
            hub.take_once(rows[1])
        dlg.close()
        win.navigate("首页")
        assert rt.monitoring.tasks[scope] is task and task.running and task.recording
        assert task.requests == (req,) and forbidden == []
    finally:
        rt.monitoring.stop(scope).result(8)


def pick_waveform_levels(workspace, qtbot, adapter):
    from lab_control.ui.pages.watch_picker import WatchPicker

    win, rt, page, _, _ = workspace
    hub = page.hub
    dev = rt.add(ConnectionConfig("mock://waveform-levels", adapter, "mock", expected_serial="88"))
    rt.connect(dev).result(5)
    win.controller.refresh_snapshot()
    picker = WatchPicker(hub, page)
    qtbot.addWidget(picker)
    signals = {}
    for leaf, record, ch, spec, _ in picker.entries:
        if record["id"] == dev:
            signals[ch, spec.metric] = (leaf.text(0), leaf.text(1), spec.source.key)
            if ch in (2, 4):
                leaf.setCheckState(0, Qt.CheckState.Checked)
    assert signals == {
        (ch, metric): (label, "V", "waveform")
        for ch in range(1, 5)
        for metric, label in (("mean", "平均值"), ("peak_to_peak", "峰峰值"))
    }
    picker.accept()
    hub.save([Preset("波形读数", tuple(picker.result_rows))])
    rows = {(row.channel, row.metric): row for row in picker.result_rows}
    assert set(rows) == {(ch, metric) for ch in (2, 4) for metric in ("mean", "peak_to_peak")}
    return dev, rows


def test_8254m_watch_waveform_levels_on_two_channels(workspace, qtbot):
    import numpy as np

    _, rt, page, _, _ = workspace
    dev, rows = pick_waveform_levels(workspace, qtbot, "lecroy.8254m")
    rt.monitoring.start(dev, (AcquisitionRequest("waveform", (2, 4), points=256),), interval=0.5)
    qtbot.waitUntil(lambda: all(row.id in page.hub.values for row in rows.values()), timeout=8000)
    for m in rt.monitoring.latest(dev, "waveform").batch.measurements:
        for metric, value in (("mean", np.mean(m.values)), ("peak_to_peak", np.ptp(m.values))):
            assert page.hub.values[rows[m.channel, metric].id][1].value == pytest.approx(value)


def test_moku_watch_waveform_levels_follow_physical_input(workspace, qtbot):
    from lab_control.domain.instruments import DeviceCommand
    from lab_control.instruments.devices.liquid_moku_pro.adapter import Adapter
    from lab_control.instruments.devices.liquid_moku_pro.tests.test_protocol import Wire

    win, rt, page, _, _ = workspace
    hub = page.hub
    wire = Wire()
    # The scope returns logical trace 2 for physical input 4. The spectrum uses that same input.
    wire.routes.update(Slot1InA="Input4", Slot3InA="Input4")
    rt.sessions.factory = lambda config: Adapter(config, wire)
    dev, rows = pick_waveform_levels(workspace, qtbot, "liquid.moku_pro")
    rt.monitoring.apply(dev, DeviceCommand("attach_workspace", confirmed=True)).result(3)
    rt.monitoring.start_role(dev, AcquisitionRequest("spectrum", (4,)))
    qtbot.waitUntil(lambda: rt.monitoring.latest(dev, "spectrum") is not None)
    hub.tick()
    assert not hub.metric_jobs and all(row.id not in hub.values for row in rows.values())
    rt.monitoring.start_role(dev, AcquisitionRequest("waveform", (4,)))
    mean, vpp = rows[4, "mean"].id, rows[4, "peak_to_peak"].id
    qtbot.waitUntil(lambda: mean in hub.values and vpp in hub.values, timeout=8000)
    assert hub.values[mean][1].value == pytest.approx(0.05 / 3)
    assert hub.values[vpp][1].value == pytest.approx(0.05)
    assert all(rows[2, metric].id not in hub.values for metric in ("mean", "peak_to_peak"))
    rt.monitoring.stop(dev).result(5)
    win.controller.poll()
    wire.frame = {"time": [0, 1, 2], "ch2": [-1, 1, 3]}
    hub.take_once(rows[4, "mean"])
    qtbot.waitUntil(lambda: hub.values[mean][1].value == 1 and hub.values[vpp][1].value == 4)
    assert all("set_connections" not in path for _, path, _ in wire.calls)


def test_slot_replacement_keeps_row_and_revalidates_unit(workspace, qtbot, monkeypatch):
    win, rt, page, meter, scope = workspace
    hub = page.hub
    spec = hub.catalog(meter)[1][("reading", "", "")]
    row = spec.row(win.controller.records[meter], 204, {})
    p = Preset("位置", (row,))
    hub.save([p])
    qtbot.waitUntil(lambda: row.id in hub.values)
    win.navigate("首页")
    rt.monitoring.stop(meter).result(5)
    replacement = rt.add(ConnectionConfig("mock://replacement", "keithley.2701", "mock"))
    rt.connect(replacement).result(5)
    rt.bind_slot(replacement, row.slot, "general", replace_device_id=meter)
    win.controller.refresh_snapshot()
    win.navigate("监视列表")
    qtbot.waitUntil(lambda: hub.values.get(row.id, (None,))[0] == replacement)
    assert hub.presets.get(p.id).rows[0].channel == 204
    channels, specs = hub.catalog(replacement)
    reconfigured = dict(specs)
    reconfigured[spec.key] = replace(spec, unit="Ω", quantity="resistance")
    monkeypatch.setattr(hub, "catalog", lambda _dev: (channels, reconfigured))
    hub.tick()
    assert hub.status[row.id] == "物理量或单位不兼容"
    assert hub.presets.get(p.id).rows[0].unit == "°C"
    assert not rt.monitoring.tasks[replacement].running


def test_recording_follows_preset_across_views_and_reopens_history(workspace, qtbot):
    import csv
    import sqlite3
    from contextlib import closing

    from lab_control.ui.pages.watch_table import PresetTable

    win, rt, page, meter, _ = workspace
    hub = page.hub
    row = hub.catalog(meter)[1][("reading", "", "")].row(win.controller.records[meter], 204, {})
    preset = Preset("温度记录", (row,))
    hub.save([preset])
    popup = page.open_window(preset.id)
    page.table.record.click()
    run = hub.recordings[preset.id]
    qtbot.waitUntil(lambda: run.count >= 1, timeout=8000)
    assert popup.findChild(PresetTable).record.text() == "停止记录"
    task = rt.monitoring.tasks[meter]
    popup.close()
    win.navigate("首页")
    first = run.count
    qtbot.waitUntil(lambda: run.count > first, timeout=9000)
    assert rt.monitoring.tasks[meter] is task
    win.navigate("监视列表")
    page.table.record.click()
    qtbot.waitUntil(run.done.is_set)
    assert not run.error
    with closing(sqlite3.connect(run.path)) as db:
        assert db.execute("SELECT state FROM batch").fetchone()[0] == "complete"
        points = db.execute("SELECT value,unit,stamp,session FROM points WHERE value IS NOT NULL").fetchall()
    assert len(points) == run.count >= 2 and len({p[2] for p in points}) >= 2
    assert {(p[0], p[1], p[3]) for p in points} == {(22.000001, "°C", rt.sessions.sessions[meter].session_id)}
    with next(run.path.parent.glob("*.csv")).open(encoding="utf-8-sig", newline="") as stream:
        assert len(list(csv.DictReader(stream))) == len(points)
    hub.recordings.clear()  # Same on-disk history available to a fresh runtime.
    index = page.table.model.index(0, 3)
    pos = page.table.table.visualRect(index).center()
    qtbot.mouseClick(page.table.table.viewport(), Qt.MouseButton.LeftButton, pos=pos)
    qtbot.mouseDClick(page.table.table.viewport(), Qt.MouseButton.LeftButton, pos=pos)
    dlg = hub.trends[(preset.id, row.id)]
    qtbot.waitUntil(lambda: len(dlg.points[0]) >= 2, timeout=5000)
    assert dlg.batches.currentData() == run.path
    assert dlg.points[2] == "°C"


def test_single_sided_limits():
    row = WatchRow("dev", "", "meter", 1, "reading", "温度", lower=0)
    assert bounds_status(0, row) == "正常" and bounds_status(-0.001, row) == "低于下限"
    assert bounds_status(100, row) == "正常"


def test_disconnect_reconnect_keeps_timestamp_and_preset_order(workspace, qtbot):
    win, rt, page, meter, _ = workspace
    hub = page.hub
    spec = hub.catalog(meter)[1][("reading", "", "")]
    row = spec.row(win.controller.records[meter], 204, {})
    first, second = Preset("机箱", (row,)), Preset("第二组")
    hub.save([first, second])
    popup = page.open_window(first.id)
    qtbot.waitUntil(lambda: row.id in hub.values)
    hub.presets.update(first.id, name="温度读数")
    hub.changed.emit()
    assert "温度读数" in popup.windowTitle()
    assert page.list.model().moveRows(QModelIndex(), 1, 1, QModelIndex(), 0)
    assert [p.id for p in Presets(rt.repository).items] == [second.id, first.id]
    rt.monitoring.stop(meter).result(5)
    rt.sessions.disconnect(meter)
    win.controller.poll()
    qtbot.waitUntil(lambda: hub.status[row.id] == "断线")
    old = hub.values[row.id][1]
    assert old.timestamp and old.value > 22
    rt.connect(meter).result(5)
    win.controller.poll()
    qtbot.waitUntil(
        lambda: hub.status.get(row.id) == "正常" and hub.values[row.id][1].received > old.received
    )
    hub.save([second])
    assert not popup.isVisible()


def test_watch_uses_only_last_thirty_minutes_and_requires_two_samples(workspace):
    import time

    from lab_control.application.controls import DataSource
    from lab_control.application.monitoring_state import MonitorFrame
    from lab_control.application.watch_measurements import RecordedWindows, compute_metric
    from lab_control.application.watchlists import SignalSpec
    from lab_control.domain.instruments import MeasurementBatch
    from lab_control.domain.temperature_metrics import TEMPERATURE_STABILITY_PROFILE

    _, rt, _, meter, _ = workspace
    session = rt.sessions.sessions[meter]
    history = RecordedWindows(session.session_id, 1)
    request = AcquisitionRequest("scalar", (204,))
    spec = SignalSpec(
        DataSource("reading", "温度", "scalar"),
        TEMPERATURE_STABILITY_PROFILE,
        "peak_to_peak",
        "峰峰值",
        "°C",
        "",
    )
    now = time.perf_counter()

    def add(age, value):
        frame = MonitorFrame(
            0,
            1,
            request,
            MeasurementBatch(session.identity, (Measurement("temperature", "°C", (value,), 204),), {}),
            now - age,
            session.session_id,
        )
        history.add({"request": request, "batch": frame.batch, "received_at": frame.received_at})
        return frame

    add(1801, 100)
    frame = add(30, 21)
    (recent,) = history.snapshot(False)
    assert recent.count == 1
    assert not compute_metric(spec, frame, 204, {}, ((recent.minimum,), recent.unit))
    frame = add(0, 24)
    (recent,) = history.snapshot(False)
    assert recent.count == 2
    result = compute_metric(spec, frame, 204, {}, ((recent.minimum, recent.maximum), recent.unit))
    assert result["peak_to_peak"].value == 3


def test_attaches_to_direct_recorder_without_claiming_it(workspace, qtbot):
    win, rt, page, meter, _ = workspace
    hub = page.hub
    request = AcquisitionRequest("scalar", (204,), confirmed=True)
    run = rt.sessions.start_acquisition(meter, request, interval=0.4, count=8)
    original_worker = rt.sessions.sessions[meter].stream_future
    row = hub.catalog(meter)[1][("reading", "", "")].row(win.controller.records[meter], 204, {})
    preset = Preset("独立记录", (row,))
    hub.save([preset])
    trend = hub.start_recording(preset.id)
    qtbot.waitUntil(lambda: row.id in hub.values, timeout=5000)
    assert not run.done()
    qtbot.waitUntil(lambda: trend.count >= 2, timeout=5000)
    hub.stop_recording(preset.id)
    qtbot.waitUntil(trend.done.is_set)
    assert not trend.error
    assert meter not in rt.monitoring.tasks
    assert hub.values[row.id][1].unit == "°C"
    win.navigate("首页")
    assert original_worker is not None and rt.sessions.sessions[meter].stream_future is original_worker
    run.result(8)


def test_scalar_matches_correct_measurement_on_shared_channel():
    from lab_control.application.controls import DataSource
    from lab_control.application.watch_measurements import select_sample
    from lab_control.domain.instruments import DeviceIdentity, MeasurementBatch

    current = Measurement("current", "A", [0.42], 1)
    voltage = Measurement("voltage", "V", [8.6], 1)
    batch = MeasurementBatch(DeviceIdentity("example", "supply", "1", "1"), (current, voltage), {})
    assert select_sample(batch, 1, DataSource("voltage", "电压", "scalar", measurement="voltage")) is voltage
    assert select_sample(batch, 1, DataSource("current", "电流", "scalar", quantity="current")) is current


def test_threshold_editor_keeps_draft_as_readings_refresh(workspace, qtbot):
    from PySide6.QtWidgets import QLineEdit, QStyleOptionViewItem

    from lab_control.ui.pages.watch_table import PresetTable

    win, rt, page, meter, _ = workspace
    hub = page.hub
    row = hub.catalog(meter)[1][("reading", "", "")].row(win.controller.records[meter], 204, {})
    preset = Preset("测温", (row,))
    hub.save([preset])
    popup = page.open_window(preset.id)
    detached = popup.findChild(PresetTable)
    qtbot.waitUntil(lambda: row.id in hub.values)

    def enter(view, column, first, rest, *, click=False, cancel=False):
        idx = view.model().index(0, column)
        view.scrollTo(idx)
        if click:
            qtbot.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=view.visualRect(idx).center())
        else:
            view.edit(idx)
        editor = view.focusWidget()
        assert isinstance(editor, QLineEdit)
        editor.selectAll()
        qtbot.keyClicks(editor, first)
        hub.updated.emit()
        assert editor.text() == first
        qtbot.keyClicks(editor, rest)
        qtbot.keyClick(editor, Qt.Key.Key_Escape if cancel else Qt.Key.Key_Return)
        qtbot.waitUntil(lambda: view.state() != view.State.EditingState)

    enter(detached.table, 5, "-", "25.75")
    assert page.table.model.rows[0].lower == -25.75
    enter(page.table.table, 6, "3.", "125", click=True)
    assert detached.model.rows[0].upper == 3.125
    qtbot.waitUntil(lambda: hub.status[row.id] == "高于上限")
    option = QStyleOptionViewItem()
    detached.table.itemDelegate().initStyleOption(option, detached.model.index(0, 3))
    assert option.palette.highlightedText().color() == detached.model.index(0, 3).data(
        Qt.ItemDataRole.ForegroundRole
    )
    enter(detached.table, 6, "4", "0", click=True, cancel=True)
    assert Presets(rt.repository).get(preset.id).rows[0].upper == 3.125
    # A draft on one bound survives saving the other bound in another view.
    idx = detached.model.index(0, 5)
    detached.table.edit(idx)
    editor = detached.table.focusWidget()
    assert isinstance(editor, QLineEdit)
    editor.selectAll()
    qtbot.keyClick(editor, Qt.Key.Key_Backspace)
    assert page.table.model.setData(page.table.model.index(0, 6), "42")
    assert editor.isVisible() and editor.text() == ""
    qtbot.keyClick(editor, Qt.Key.Key_Return)
    qtbot.waitUntil(lambda: detached.table.state() != detached.table.State.EditingState)
    saved = Presets(rt.repository).get(preset.id).rows[0]
    assert saved.lower is None and saved.upper == 42


def test_signal_picker_starts_collapsed_and_search_preserves_checks(workspace, qtbot):
    from lab_control.ui.pages.watch_picker import WatchPicker, children

    _, _, page, _, _ = workspace
    picker = WatchPicker(page.hub, page)
    qtbot.addWidget(picker)
    picker.show()
    devices = list(children(picker.tree.invisibleRootItem()))
    assert len(devices) == 2 and all(not d.isExpanded() for d in devices)
    picker.search.setText("RMS")
    (device,) = [d for d in devices if not d.isHidden()]
    assert device.isExpanded()
    channel = device.child(0)
    assert channel.isExpanded()
    (signal,) = [s for s in children(channel) if not s.isHidden()]
    signal.setCheckState(0, Qt.CheckState.Checked)
    picker.search.clear()
    assert all(not d.isHidden() and not d.isExpanded() for d in devices)
    assert signal.checkState(0) == Qt.CheckState.Checked
    picker.accept()
    assert len(picker.result_rows) == 1 and picker.result_rows[0].metric == "rms"


def test_pinning_preserves_manual_order_and_preset_identity(workspace, monkeypatch):
    from lab_control.ui.widgets.popup_surface import WorkspaceMenu

    win, rt, page, _, _ = workspace
    hub = page.hub
    a, b, c, d = (Preset(name) for name in "ABCD")
    hub.save((a, b, c, d))

    def order():
        return [page.key(page.list.item(i)) for i in range(page.list.count())]

    def menu_action(label):
        monkeypatch.setattr(
            WorkspaceMenu, "exec", lambda m, _: next(x for x in m.actions() if x.text() == label)
        )
        page.menu(page.list.visualItemRect(page.list.currentItem()).center())

    page.pin_button.click()
    page.select_id(b.id)
    assert hub.presets.pinned.id == a.id and page.pin_button.text() == "置顶到首页"
    page.pin_button.click()
    assert order() == [b.id, a.id, c.id, d.id] and page.pin_button.text() == "取消置顶"
    assert [not page.list.item(i).icon().isNull() for i in range(4)] == [True, False, False, False]
    assert page.list.model().moveRows(QModelIndex(), 3, 1, QModelIndex(), 1)
    assert order() == [b.id, d.id, a.id, c.id]
    assert page.list.model().moveRows(QModelIndex(), 2, 1, QModelIndex(), 0)
    assert order() == [b.id, a.id, d.id, c.id]
    page.select_id(b.id)
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("已改名", True))
    menu_action("重命名")
    assert hub.presets.pinned.name == "已改名"
    menu_action("复制预设")
    clone = hub.presets.get(page.key(page.list.currentItem()))
    assert clone.id != b.id and not clone.pinned and clone.name == "已改名 副本"
    assert Presets(rt.repository).pinned.id == b.id
    page.select_id(b.id)
    page.pin_button.click()
    assert order() == [a.id, b.id, d.id, c.id, clone.id]
    assert Presets(rt.repository).pinned is None
    win.navigate("首页")
    card = win.overview_side.watch_card
    assert card.empty.isVisible() and "选择" in card.empty.text()
    card.open_button.click()
    page.pin_button.click()
    win.navigate("首页")
    assert card.preset_name.text() == "已改名" and "添加" in card.empty.text()
    card.open_button.click()
    monkeypatch.setattr(QMessageBox, "question", lambda *a: QMessageBox.StandardButton.Yes)
    menu_action("删除预设")
    win.navigate("首页")
    assert card.empty.isVisible() and "选择" in card.empty.text()
    assert Presets(rt.repository).pinned is None


def test_home_readings_share_live_task_and_alarm_state(workspace, qtbot):
    from lab_control.ui.pages.watch_table import PresetTable

    win, rt, page, meter, _ = workspace
    hub = page.hub
    spec = hub.catalog(meter)[1][("reading", "", "")]
    rows = tuple(replace(spec.row(win.controller.records[meter], 204, {}), upper=22) for _ in range(40))
    first, other = Preset("试验箱温度", rows, pinned=True), Preset("另一组")
    hub.save([other, first])
    page.select_id(first.id)
    dlg = page.open_window(first.id)
    detached = dlg.findChild(PresetTable)
    qtbot.waitUntil(lambda: hub.status.get(rows[0].id) == "高于上限")
    task = rt.monitoring.tasks[meter]
    win.navigate("首页")
    card = win.overview_side.watch_card
    assert card.isVisible() and card.model.preset == first.id and card.preset_name.text() == first.name
    qtbot.waitUntil(lambda: card.list.verticalScrollBar().maximum() > 0)
    assert hub.views == {card: first.id, detached: first.id}
    assert card.model.rows == rows
    for col in (0, 1, 2, 3, 4, 7, 8):
        assert card.model.index(0, col).data() == detached.model.index(0, col).data()
    assert "高于上限" in card.model.index(0, 0).data(Qt.ItemDataRole.AccessibleTextRole)
    assert card.model.index(0, 3).data(Qt.ItemDataRole.ForegroundRole) == detached.model.index(0, 8).data(
        Qt.ItemDataRole.ForegroundRole
    )
    assert len(win.chrome.watchlist_alarms()) == len(rows)
    win.refresh()
    alert_count = win._overview.alert_count
    assert "40" in alert_count.text()
    qtbot.waitUntil(lambda: alert_count.width() >= alert_count.sizeHint().width())
    dlg.close()
    assert hub.views == {card: first.id} and task.running
    assert rt.monitoring.tasks[meter] is task and len(task.requests) == 1
    rt.monitoring.stop(meter).result(5)
    with rt.monitoring.lock:
        for key, frame in tuple(task.frames.items()):
            task.frames[key] = replace(frame, received_at=frame.received_at - 120)
    hub.tick()
    assert card.model.index(0, 8).data() == "已过期"
    assert win.chrome.watchlist_alarms() == []
    rt.sessions.disconnect(meter)
    win.controller.poll()
    qtbot.waitUntil(lambda: card.model.index(0, 8).data() == "断线")
    assert win.chrome.watchlist_alarms() == []
    rt.connect(meter).result(5)
    win.controller.poll()
    qtbot.waitUntil(lambda: card.model.index(0, 8).data() == "高于上限")
    page.select_id(other.id)
    task = rt.monitoring.tasks[meter]
    card.open_button.click()
    assert page.isVisible() and page.key(page.list.currentItem()) == first.id
    assert hub.views == {page.table: first.id}
    win.controller.poll()
    assert rt.monitoring.tasks[meter] is task and task.running
    page.select_id(other.id)
    qtbot.waitUntil(lambda: not task.running)


def test_home_loads_persisted_pin_without_opening_editor(tmp_path, qtbot):
    original = ApplicationRuntime(root=tmp_path, hardware=False)
    try:
        original.repository.save_workspace(Presets.key, [{"name": "旧预设", "id": "legacy", "rows": []}])
        presets = Presets(original.repository)
        assert presets.pinned is None
        meter = original.add(ConnectionConfig("mock://startup-meter", "keysight.3458a", "mock"))
        row = WatchRow(meter, "", "keysight.3458a", 1, "reading", "读数", upper=0)
        presets.save((*presets.items, Preset("启动读数", (row,), id="home")))
        presets.pin("home")
    finally:
        original.close()
    rt = ApplicationRuntime(root=tmp_path, hardware=False)
    rt.sessions.factory = SimulatedInstrument
    try:
        rt.connect(meter).result(5)
        win = RealWindow(rt)
        qtbot.addWidget(win)
        win.show()
        try:
            card = win.overview_side.watch_card
            assert win.current_module == "首页" and ("监视列表", "") not in win.page_map
            qtbot.waitUntil(lambda: card.model.index(0, 8).data() == "高于上限", timeout=8000)
            assert win.chrome.watchlist_alarms()[0]["preset_id"] == "home"
            qtbot.mouseClick(win._overview.alert_count, Qt.MouseButton.LeftButton)
            assert win.stack.currentWidget().table.model.preset == "home"
            win.navigate("首页")
            win.chrome.alerts.click()
            assert win.stack.currentWidget().table.model.preset == "home"
        finally:
            win.close()
            qtbot.waitUntil(lambda: win.closing, timeout=8000)
    finally:
        rt.close()


def test_home_cards_share_available_height_and_scroll_independently(workspace, qtbot, monkeypatch):
    win, _, page, meter, _ = workspace
    win.navigate("首页")
    win.timer.stop()
    win.controller.timer.stop()
    side = win.overview_side
    card = side.watch_card
    side.refresh({})
    win.resize(1330, 780)
    qtbot.waitUntil(lambda: abs(card.height() - side.tasks.height()) <= 1)
    heights = (card.height(), side.tasks.height())
    assert card.title.text() == "状态监视" and side.tasks.title.text() == "采集任务"
    assert side.task_empty.isVisible()
    rows = tuple(WatchRow("offline", "", "meter", ch, "reading", "电压") for ch in range(1, 26))
    page.hub.save([Preset("全部测点", rows, pinned=True)])
    tasks = {}
    for index in range(15):
        key = f"scroll-{index:02}"
        monkeypatch.setitem(win.controller.records, key, dict(win.controller.records[meter], id=key))
        tasks[key] = {"running": True}
    side.refresh(tasks)
    reads = card.list.verticalScrollBar()
    jobs = side.task_scroll.verticalScrollBar()
    qtbot.waitUntil(lambda: reads.maximum() > 0 and jobs.maximum() > 0)
    assert (card.height(), side.tasks.height()) == heights
    assert side.task_count.text() == "15 个运行中"
    assert [row.device_id for row in side.task_rows if row.isVisible()] == list(tasks)
    reads.setValue(reads.maximum())
    assert jobs.value() == 0
    side.task_scroll.ensureWidgetVisible(side.task_rows[-1])
    assert jobs.value() > 0 and reads.value() == reads.maximum()
    side.refresh(tasks)
    assert jobs.value() > 0 and reads.value() == reads.maximum()
    first = next(iter(tasks))
    side.refresh({first: {"running": True}})
    qtbot.waitUntil(lambda: jobs.maximum() == 0)
    assert (card.height(), side.tasks.height()) == heights
    assert [row.device_id for row in side.task_rows if row.isVisible()] == [first]
    assert card.geometry().bottom() < side.tasks.geometry().top()
    assert side.tasks.geometry().bottom() == side.height() - 1
    win.resize(1440, 960)
    qtbot.waitUntil(lambda: card.height() > heights[0] and side.tasks.height() > heights[1])
    assert abs(card.height() - side.tasks.height()) <= 1
    assert side.tasks.geometry().bottom() == side.height() - 1
    win.resize(1280, 760)
    qtbot.waitUntil(lambda: card.height() < heights[0] and side.tasks.height() < heights[1])
    assert abs(card.height() - side.tasks.height()) <= 1
    assert card.list.verticalScrollBar().maximum() > 0
