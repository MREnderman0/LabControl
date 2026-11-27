import csv
import sqlite3
from contextlib import closing
from dataclasses import replace
from threading import Event

from lab_control.application.controls import DataSource
from lab_control.application.monitoring_state import MonitorFrame
from lab_control.application.watch_recording import Route, WatchRecording
from lab_control.application.watchlists import Preset, SignalSpec, WatchRow
from lab_control.domain.instruments import AcquisitionRequest, DeviceIdentity, Measurement, MeasurementBatch
from lab_control.storage.watch_trends import (
    append_point,
    chart_data,
    create_batch,
    export_csv,
    history,
    preset_suffix,
)


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


def test_flat_batch_collisions_per_item_csv_and_legacy_history(tmp_path, monkeypatch):
    from datetime import datetime

    class FrozenClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 27, 18, 1, 21, tzinfo=tz)

    monkeypatch.setattr("lab_control.storage.watch_trends.datetime", FrozenClock)
    row = WatchRow("dev", "", "keithley.2701", 205, "reading", "温度", id="a")
    rows = (
        row,
        replace(row, id="b", channel=210),
        replace(row, id="c", label="平均值"),
        replace(row, id="d", label="峰峰值"),
        replace(row, id="e"),
        replace(row, id="f", label="未连接"),
    )
    preset = Preset("示例", rows)
    path = create_batch(tmp_path, preset)
    second = create_batch(tmp_path, preset)
    parent = tmp_path / "监视列表记录"
    assert path.parent == parent / "2026-11-27_18-01-21_示例"
    assert second.parent == parent / "2026-11-27_18-01-21_示例_02"
    with closing(sqlite3.connect(path)) as db, db:
        for index, r in enumerate(rows):
            stamp = f"2026-11-27T10:03:0{index}+00:00"
            value = None if index == 5 else float(index)
            append_point(
                db,
                r,
                r.device,
                "2701",
                "s",
                stamp,
                index,
                value,
                "°C" if value is not None else "",
                "断线" if value is None else "正常",
            )
        export_csv(path, db)
        for i, (r, file) in enumerate(zip(rows, sorted(path.parent.glob("*.csv")), strict=True)):
            assert file.name == f"{i + 1:02d}_2701_CH{r.channel}_{r.label}.csv"
            with file.open(encoding="utf-8-sig", newline="") as fp:
                [entry] = list(csv.DictReader(fp))
            assert entry["监视行 ID"] == r.id and entry["设备 ID"] == r.device
            saved = db.execute(
                "SELECT stamp,value,unit,status FROM points WHERE row_id=?", (r.id,)
            ).fetchone()
            assert (
                entry["采集时间"],
                float(entry["数值"]) if entry["数值"] else None,
                entry["单位"],
                entry["状态"],
            ) == saved
    legacy = parent / ("旧名_" + preset_suffix(preset.id)) / "20260101-100000_abc"
    legacy.mkdir(parents=True)
    old = second.rename(legacy / "trend.sqlite3")
    assert {p for p, *_ in history(tmp_path, preset.id, row.id)} == {path, old}


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
