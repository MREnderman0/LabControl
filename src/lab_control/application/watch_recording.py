"""Capture immutable source frames for a preset. Acquisition never waits for disk or metric calculation."""

from __future__ import annotations

import sqlite3
import threading
from collections import deque
from contextlib import closing
from dataclasses import dataclass

from lab_control.application.monitoring_state import MonitorFrame
from lab_control.application.watch_measurements import compute_metric, scalar, select_sample
from lab_control.application.watchlists import SignalSpec, WatchRow, bounds_status
from lab_control.domain.instruments import DeviceError, utc_now
from lab_control.storage.watch_trends import append_point, create_batch, export_csv, finish_batch


@dataclass(frozen=True)
class Route:
    row: WatchRow
    device: str
    name: str
    session: str
    spec: SignalSpec | None
    reason: str = ""


def compatible(spec, frame):
    return frame.request.kind == spec.source.kind or (
        spec.profile
        and spec.profile.id == "phase_noise.at_offset"
        and frame.request.kind in {"trace", "timed_phase"}
    )


def metric_window(history, optical, channel):
    if optical:
        points = next((c.points for c in history.channels if c.channel == channel), ()) if history else ()
        return tuple(p.watts for p in points), "W"
    snap = next((c for c in (history or ()) if c.channel == channel), None)
    if not snap or not snap.count:
        return (), ""
    return ((snap.minimum,) if snap.count == 1 else (snap.minimum, snap.maximum)), snap.unit


QueuedItem = tuple[tuple[Route, ...], MonitorFrame | None, dict | tuple[str, str]]


class WatchRecording:
    max_entries = 256
    max_bytes = 64 * 1024 * 1024

    def __init__(self, root, preset):
        self.preset = preset
        self.path = create_batch(root, preset)
        self.condition = threading.Condition()
        self.queue: deque[tuple[QueuedItem, int]] = deque()
        self.queued_bytes = 0
        self.routes = {}
        self.bindings = {}
        self.accepting = True
        self.done = threading.Event()
        self.error = ""
        self.count = 0
        self.revision = 0
        self.last_stamp = ""
        self.last_status = {}
        self.units = {}
        self.thread = threading.Thread(target=self._write, name="watch-trend-writer")
        self.thread.start()

    def _offer(self, item, size=0):
        # Caller holds condition. Bound both the number and size of referenced source frames.
        if not self.accepting:
            return
        if len(self.queue) >= self.max_entries or size + self.queued_bytes > self.max_bytes:
            self.error = "写入积压，已停止接收新数据"
            self.accepting = False
        else:
            self.queue.append((item, size))
            self.queued_bytes += size
        self.condition.notify()

    def bind(self, routes):
        with self.condition:
            if not self.accepting:
                return
            targets: dict[str, list[Route]] = {}
            for route in routes:
                key = route.row.id
                previous = self.bindings.get(key)
                binding = route.device, route.session, route.reason
                if previous != binding:
                    label = route.reason
                    if not label and previous is not None:
                        label = "槽位更换设备" if previous[0] != route.device else "会话重新连接"
                    if label:
                        self._offer(((route,), None, (utc_now(), label)))
                    self.bindings[key] = binding
                if route.spec and not route.reason:
                    targets.setdefault(route.device, []).append(route)
            self.routes = targets

    def capture(self, device, frame, get_history):
        with self.condition:
            routes = tuple(
                r
                for r in self.routes.get(device, ())
                if self.accepting
                and r.session == frame.session_id
                and compatible(r.spec, frame)
                and (
                    any(m.channel == r.row.channel for m in frame.batch.measurements)
                    if r.spec.profile
                    else select_sample(frame.batch, r.row.channel, r.spec.source) is not None
                )
            )
        if not routes:
            return
        # Capture each rolling snapshot at its source frame, before the next frame advances it.
        histories = {}
        size = sum(
            (a.nbytes if hasattr(a, "nbytes") else len(a) * 8)
            for m in frame.batch.measurements
            for a in (m.values, m.axis)
            if a is not None
        )
        for route in routes:
            profile = route.spec.profile
            if profile and profile.supports_multiple_recordings:
                optical = profile.id.startswith("optical_power.")
                if optical not in histories:
                    history = get_history(optical)
                    histories[optical] = history
                    if optical and history:
                        size += sum(len(c.points) * 80 for c in history.channels)
        with self.condition:
            # A concurrent rebind cannot put an old frame after the boundary for its row.
            current = self.routes.get(device, ())
            routes = tuple(r for r in routes if r in current)
            if routes:
                self._offer((routes, frame, histories), size)

    def stop(self):
        with self.condition:
            self.accepting = False
            self.condition.notify()

    def _gap(self, db, route, stamp, message):
        key = route.row.id
        identity = (message, route.device, route.session)
        if self.last_status.get(key) != identity:
            append_point(
                db, route.row, route.device, route.name, route.session, stamp, None, None, "", message
            )
            self.last_status[key] = identity

    def _save(self, db, item):
        routes, frame, context = item
        count, last = 0, ""
        metrics = {}
        for route in routes:
            row, spec = route.row, route.spec
            if frame is None:
                self._gap(db, route, *context)
                continue
            if spec.profile:
                key = row.channel, row.source, row.profile, repr(sorted(row.parameters.items()))
                if key not in metrics:
                    window = None
                    if spec.profile.supports_multiple_recordings:
                        optical = row.profile.startswith("optical_power.")
                        window = metric_window(context.get(optical), optical, row.channel)
                    try:
                        metrics[key] = compute_metric(spec, frame, row.channel, row.parameters, window)
                    except (DeviceError, ValueError) as exc:
                        self._gap(db, route, frame.batch.timestamp, str(exc))
                        continue
                reading = metrics[key].get(row.metric)
            else:
                reading = scalar(frame, row.channel, spec.source)
            if reading is None:
                self._gap(db, route, frame.batch.timestamp, "等待有效样本")
                continue
            expected = self.units.get(row.id, (row.quantity, row.unit))
            if any(a and a != b for a, b in zip(expected, (reading.quantity, reading.unit))):
                self._gap(db, route, reading.timestamp, "物理量或单位变化")
                continue
            self.units[row.id] = reading.quantity, reading.unit
            received = frame.batch.performance.get("received_at_monotonic", frame.received_at)
            inserted = append_point(
                db,
                row,
                route.device,
                route.name,
                route.session,
                reading.timestamp,
                received,
                reading.value,
                reading.unit,
                bounds_status(reading.value, row),
            )
            if inserted:
                count += inserted
                last = reading.timestamp
                self.last_status.pop(row.id, None)
        return count, last

    def _write(self):
        try:
            with closing(sqlite3.connect(self.path)) as db:
                try:
                    while True:
                        with self.condition:
                            self.condition.wait_for(lambda: self.queue or not self.accepting)
                            if not self.queue:
                                break
                            items = []
                            for _ in range(min(32, len(self.queue))):
                                item, size = self.queue.popleft()
                                self.queued_bytes -= size
                                items.append(item)
                        count, last = 0, ""
                        with db:
                            for item in items:
                                n, stamp = self._save(db, item)
                                count += n
                                last = stamp or last
                        self.count += count
                        self.revision += 1
                        self.last_stamp = last or self.last_stamp
                    export_csv(self.path, db)
                except Exception as exc:
                    self.error = f"记录写入失败：{exc}"
                finish_batch(db, "error" if self.error else "complete", self.error)
        except Exception as exc:
            self.error = f"记录未正常完成：{exc}"
        finally:
            with self.condition:
                self.accepting = False
                self.queue.clear()
                self.routes.clear()
                self.queued_bytes = 0
            self.done.set()
