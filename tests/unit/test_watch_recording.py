import sqlite3
from contextlib import closing
from dataclasses import replace
from threading import Event

from lab_control.application.controls import DataSource
from lab_control.application.monitoring_state import MonitorFrame
from lab_control.application.watch_recording import Route, WatchRecording
from lab_control.application.watchlists import Preset, SignalSpec, WatchRow
from lab_control.domain.instruments import AcquisitionRequest, DeviceIdentity, Measurement, MeasurementBatch
from lab_control.storage.watch_trends import chart_data, history


def setup_run(root):
    source = DataSource("reading", "温度", "scalar", measurement="temperature")
    spec = SignalSpec(source, None, "", "温度", "°C", "temperature")
    row = WatchRow("meter", "", "example", 1, "reading", "温度", unit="°C", upper=20)
    preset = Preset("测温", (row,))
    run = WatchRecording(root, preset)
    route = Route(row, "meter", "仪器 A", "session", spec)
    run.bind([route])
    return run, route


def sample(i, value=22, unit="°C", device_session="session"):
    identity = DeviceIdentity("lab", "meter", "123", "1")
    stamp = f"2026-01-01T12:00:{i:02d}+00:00"
    batch = MeasurementBatch(
        identity,
        (Measurement("temperature", unit, (value,), 1, metadata={"received_at": stamp}),),
        {},
        timestamp=stamp,
    )
    return MonitorFrame(
        i, 1, AcquisitionRequest("scalar", (1,), confirmed=True), batch, float(i), device_session
    )


def test_each_source_reading_once_and_gaps(tmp_path):
    run, route = setup_run(tmp_path)
    try:
        first = sample(1)
        run.capture("meter", first, None)
        run.capture("meter", first, None)
        for frame in [sample(2), sample(3, unit="K"), sample(4, unit="K"), sample(5)]:
            run.capture("meter", frame, None)
        run.bind([replace(route, reason="断线", spec=None)])
        run.capture("meter", sample(6), None)
        run.bind([replace(route, session="new")])
        run.capture("meter", sample(7), None)
        run.capture("meter", sample(8, device_session="new"), None)
    finally:
        run.stop()
        run.thread.join(3)
    assert run.done.is_set() and not run.error
    assert run.count == 4 and run.last_stamp == sample(8).batch.timestamp
    with closing(sqlite3.connect(run.path)) as db:
        assert db.execute("SELECT status FROM points ORDER BY id").fetchall() == [
            ("高于上限",),
            ("高于上限",),
            ("物理量或单位变化",),
            ("高于上限",),
            ("断线",),
            ("会话重新连接",),
            ("高于上限",),
        ]
    assert history(tmp_path, run.preset.id, route.row.id)[0][2] == "complete"
    x, y, unit, note = chart_data(run.path, route.row.id, pixels=2)
    assert x == sorted(x) and unit == "°C" and note and any(n != n for n in y)


def test_overflow_drains_accepted_samples(tmp_path, monkeypatch):
    run, route = setup_run(tmp_path)
    gate, working = Event(), Event()
    original = run._save

    def delayed(db, item):
        working.set()
        assert gate.wait(3)
        return original(db, item)

    monkeypatch.setattr(run, "_save", delayed)
    run.max_entries = 1
    try:
        run.capture("meter", sample(1), None)
        assert working.wait(2)
        run.capture("meter", sample(2), None)
        run.capture("meter", sample(3), None)
        assert not run.accepting
    finally:
        gate.set()
        run.stop()
        run.thread.join(3)
    assert run.count == 2 and run.last_stamp == sample(2).batch.timestamp
    assert history(tmp_path, run.preset.id, route.row.id)[0][2] == "error"
