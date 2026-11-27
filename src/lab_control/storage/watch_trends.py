"""Durable watchlist batches. Each directory can be read independently of the inventory."""

from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import asdict
from datetime import datetime
from itertools import chain, count
from pathlib import Path
from tempfile import NamedTemporaryFile

from lab_control.domain.instruments import utc_now
from lab_control.storage.paths import filename_part


def preset_suffix(key):
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def create_batch(root, preset):
    started = datetime.now().astimezone()
    parent = root / "监视列表记录"
    parent.mkdir(parents=True, exist_ok=True)
    name = started.strftime("%Y-%m-%d_%H-%M-%S_") + filename_part(preset.name)
    for number in count(1):
        folder = parent / (name if number == 1 else f"{name}_{number:02d}")
        try:
            folder.mkdir()
            break
        except FileExistsError:
            continue
    path = folder / "trend.sqlite3"
    pending = path.with_suffix(".creating")
    with closing(sqlite3.connect(pending)) as db, db:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript("""
            CREATE TABLE batch(preset TEXT, name TEXT, started TEXT, finished TEXT, state TEXT, error TEXT);
            CREATE TABLE rows(id TEXT PRIMARY KEY, label TEXT, channel INTEGER, spec TEXT);
            CREATE TABLE points(id INTEGER PRIMARY KEY, row_id TEXT, device TEXT, name TEXT, session TEXT,
                stamp TEXT, epoch REAL, received REAL, value REAL, unit TEXT, status TEXT);
            CREATE UNIQUE INDEX frame_once ON points(row_id,device,session,received) WHERE received IS NOT NULL;
            CREATE INDEX chart_range ON points(row_id,epoch,id);
            CREATE INDEX csv_sequence ON points(row_id,id);
            PRAGMA user_version=2;
        """)
        db.execute(
            "INSERT INTO batch VALUES(?,?,?,'','open','')", (preset.id, preset.name, started.isoformat())
        )
        db.executemany(
            "INSERT INTO rows VALUES(?,?,?,?)",
            ((r.id, r.label, r.channel, json.dumps(asdict(r), ensure_ascii=False)) for r in preset.rows),
        )
    pending.replace(path)
    return path


def reading_db(path):
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)


def history(root, preset_id, row_id):
    batches = []
    parent = root / "监视列表记录"
    paths = chain(
        parent.glob("*/trend.sqlite3"), parent.glob("*_" + preset_suffix(preset_id) + "/*/trend.sqlite3")
    )
    for path in paths:
        with closing(reading_db(path)) as db:
            batch = db.execute(
                "SELECT started,state,error FROM batch WHERE preset=? AND EXISTS "
                "(SELECT 1 FROM rows WHERE id=?)",
                (preset_id, row_id),
            ).fetchone()
        if batch:
            batches.append((path, *batch))
    return sorted(batches, key=lambda b: b[1], reverse=True)


def append_point(db, row, device, name, session, timestamp, received, value, unit, status):
    epoch = datetime.fromisoformat(timestamp).timestamp()
    cur = db.execute(
        "INSERT INTO points(row_id,device,name,session,stamp,epoch,received,value,unit,status) "
        "VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
        (row.id, device, name, session, timestamp, epoch, received, value, unit, status),
    )
    return cur.rowcount


CSV_COLUMNS = (
    "采集时间",
    "设备",
    "设备 ID",
    "通道／测点",
    "监视量",
    "数值",
    "单位",
    "状态",
    "会话 ID",
    "监视行 ID",
)


def row_filename(position, row):
    model = filename_part(row["adapter"].rsplit(".", 1)[-1].upper())
    label = filename_part(row["label"])
    return f"{position:02d}_{model}_CH{row['channel']}_{label}.csv"


def write_row_csv(db, row_id, target: Path, through_id: int):
    """Publish one complete row export. The cap fixes the snapshot while acquisition continues."""
    temporary = None
    try:
        with NamedTemporaryFile(
            dir=target.parent,
            prefix=".watch-",
            suffix=".tmp",
            mode="w",
            encoding="utf-8-sig",
            newline="",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            writer = csv.writer(stream)
            writer.writerow(CSV_COLUMNS)
            writer.writerows(
                db.execute(
                    "SELECT p.stamp,p.name,p.device,r.channel,r.label,p.value,p.unit,p.status,p.session,r.id "
                    "FROM points p JOIN rows r ON r.id=p.row_id WHERE p.row_id=? AND p.id<=? ORDER BY p.id",
                    (row_id, through_id),
                )
            )
        temporary.replace(target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def last_point_id(db):
    return db.execute("SELECT COALESCE(MAX(id),0) FROM points").fetchone()[0]


def export_csv(path, db):
    through_id = last_point_id(db)
    # Immutable rows are inserted in the preset's start order.
    for position, (row_id, spec) in enumerate(db.execute("SELECT id,spec FROM rows ORDER BY rowid"), 1):
        write_row_csv(db, row_id, path.with_name(row_filename(position, json.loads(spec))), through_id)


def export_row_snapshot(path, row_id, target, through_id):
    with closing(reading_db(path)) as db:
        write_row_csv(db, row_id, target, through_id)


def finish_batch(db, state, error):
    with db:
        db.execute("UPDATE batch SET finished=?,state=?,error=?", (utc_now(), state, error))


def chart_data(path: Path, row_id: str, limits=None, pixels=1200):
    """Reduce for display only, retaining extrema and gaps. The file and CSV retain every point."""
    with closing(reading_db(path)) as db:
        where = "row_id=?"
        args = [row_id]
        if limits:
            where += " AND epoch BETWEEN ? AND ?"
            args.extend(limits)
        sql = f"SELECT epoch,value,unit,status FROM points WHERE {where} ORDER BY epoch,id"
        lo, hi = db.execute(f"SELECT min(epoch),max(epoch) FROM points WHERE {where}", args).fetchone()
        if lo is None:
            return [], [], "", ""
        width = max((hi - lo) / max(1, pixels), 1e-9)
        x, y, unit, note = [], [], "", ""
        bucket = None
        first = None
        low = high = last = (0, 0.0, 0.0)

        def flush():
            if first is not None:
                for _, t, v in sorted({first, low, high, last}):
                    x.append(t)
                    y.append(v)

        for order, (stamp, value, u, status) in enumerate(db.execute(sql, args)):
            idx = int((stamp - lo) / width)
            if value is None or idx != bucket:
                flush()
                first = None
                bucket = idx
            if value is None:
                x.append(stamp)
                y.append(float("nan"))
                note = f"{datetime.fromtimestamp(stamp):%m-%d %H:%M:%S}  {status}"
                continue
            point = (order, stamp, value)
            if first is None:
                first = low = high = point
            if value < low[2]:
                low = point
            if value > high[2]:
                high = point
            last = point
            unit = u
        flush()
        return x, y, unit, note
