from dataclasses import replace

import pytest
from PySide6.QtCore import Qt

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
    model = page.table.model
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
        dlg = page.open_window(preset.id)
        qtbot.waitUntil(lambda: rows[0].id in hub.values, timeout=9000)
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
    from PySide6.QtCore import QModelIndex

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
    hub.save([Preset("独立记录", (row,))])
    qtbot.waitUntil(lambda: row.id in hub.values, timeout=5000)
    assert not run.done()
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
