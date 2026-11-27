"""One set of subscriptions for the watch page and all its independent windows."""

from __future__ import annotations

import json
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

from PySide6.QtCore import QObject, Signal

from lab_control.application.monitoring_state import MonitorFrame
from lab_control.application.watch_measurements import compute_metric, scalar, select_sample
from lab_control.application.watchlists import Presets, bounds_status, signal_catalog
from lab_control.domain.instruments import DeviceError
from lab_control.instruments.registry import DEVICE_DEFINITIONS
from lab_control.instruments.scope_single import SingleCapture
from lab_control.ui.pages.monitoring.timing import monitoring_options


class WatchRuntime(QObject):
    changed = Signal()
    updated = Signal()

    def __init__(self, controller, parent):
        super().__init__(parent)
        self.ctl = controller
        self.service = controller.runtime.monitoring
        self.presets = Presets(controller.runtime.repository)
        self.views = {}
        self.revisions = {}
        self.catalogs = {}
        self.values = {}
        self.status = {}
        self.devices = {}
        self.samples = {}
        self.errors = {}
        self.metric_jobs = {}
        self.metric_cache = {}
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="watch-metrics")
        self.closed = False
        self.learned = {}
        self.retry_at = {}
        self.slot_cache = {}
        self.slot_revision = -1
        controller.refreshed.connect(self.tick)

    def slot_details(self, slot):
        store = self.ctl.runtime.repository.function_slots
        if store.names_revision != self.slot_revision:
            self.slot_cache.clear()
            self.slot_revision = store.names_revision
        if slot not in self.slot_cache:
            self.slot_cache[slot] = store.label(slot, "general"), store.channel_names(slot, "general")
        return self.slot_cache[slot]

    def save(self, items):
        self.presets.save(items)
        self.changed.emit()

    def subscribe(self, view, preset):
        if self.views.get(view, "") == preset:
            return
        if preset:
            self.views[view] = preset
        else:
            self.views.pop(view, None)
        self.tick()

    def close(self):
        self.views.clear()
        self.tick()
        self.closed = True
        self.worker.shutdown(wait=False, cancel_futures=True)

    def catalog(self, dev):
        snap = self.ctl.runtime.sessions.capability_snapshot(dev)
        cached = self.catalogs.get(dev)
        if cached is None or cached[0] is not snap:
            cached = (snap, signal_catalog(self.ctl.records[dev], snap))
            self.catalogs[dev] = cached
        return cached[1]

    def resolve(self, row):
        if row.slot:
            return next(
                (d for d, r in self.ctl.records.items() if r.get("slots", {}).get("general") == row.slot), ""
            )
        return row.device if row.device in self.ctl.records else ""

    def active_rows(self):
        active = set(self.views.values())
        return [r for p in self.presets.items if p.id in active for r in p.rows]

    def requests(self, device, signals):
        grouped = defaultdict(set)
        specs = {}
        for row, spec in signals:
            source = spec.source
            if source.kind not in {"scalar", "spectrum", "trace"} or source.request_defaults.get("initiate"):
                continue
            grouped[source.key].add(row.channel)
            specs[source.key] = source
        definition = DEVICE_DEFINITIONS.get(self.ctl.records[device]["adapterId"])
        requests = []
        for key, channels in grouped.items():
            if self.catalogs[device][0].details.get("acquisition_channel_selection") == "scan_list" and (
                not definition or key not in definition.multi_channel_sources
            ):
                channels = set(self.catalog(device)[0])
            elif len(channels) > 1 and definition and definition.acquisition_channel_selection == "single":
                # Single-input devices cannot change inputs on behalf of another row.
                active = self.catalogs[device][0].details.get("active_channel")
                channels = {active if active in channels else min(channels)}
            request = specs[key].request(tuple(sorted(channels)), confirmed=True)
            if request not in requests:
                requests.append(request)
        return tuple(requests)

    def tick(self):
        if self.closed or self.ctl.runtime.closed:
            return
        active = self.active_rows()
        if not active and not self.revisions and not self.metric_jobs:
            return
        groups = defaultdict(list)
        for row in active:
            dev = self.resolve(row)
            sid = self.ctl.states.get(dev, {}).get("sessionId")
            if self.devices.get(row.id) != (dev, sid):
                self.devices[row.id] = (dev, sid)
                # A replacement must never inherit a previous instrument's sample.
                if dev and self.values.get(row.id, (None,))[0] not in (None, dev):
                    self.values.pop(row.id, None)
            if not dev or not self.ctl.has_connected_session(dev):
                self.status[row.id] = "未绑定" if not dev and row.slot else "断线"
                continue
            channels, specs = self.catalog(dev)
            spec = specs.get((row.source, row.profile, row.metric))
            if (
                row.channel not in channels
                or not spec
                or (spec.profile and spec.profile.version != row.version)
            ):
                self.status[row.id] = "通道或监视量不兼容"
                continue
            quantity = spec.profile.id if spec.profile else spec.quantity
            if (row.quantity and quantity and row.quantity != quantity) or (
                row.unit and spec.unit and row.unit != spec.unit
            ):
                self.status[row.id] = "物理量或单位不兼容"
                continue
            if dev != row.device and not (
                row.quantity or row.unit or row.profile or row.adapter == self.ctl.records[dev]["adapterId"]
            ):
                self.status[row.id] = "待核对物理量"
                continue
            groups[dev].append((row, spec))

        for dev in set(self.revisions) - groups.keys():
            self.service.sync_watch(dev, (), 0, "", self.revisions.pop(dev)[1])
            self.retry_at.pop(dev, None)
        for dev, signals in groups.items():
            session_id = self.ctl.states[dev]["sessionId"]
            if self.revisions.get(dev, (None,))[0] != session_id:
                self.revisions[dev] = (session_id, self.service.stop_revision(dev))
            timing = monitoring_options(self.ctl, dev, "general")
            if time.monotonic() >= self.retry_at.get(dev, 0):
                try:
                    self.service.sync_watch(
                        dev,
                        self.requests(dev, signals),
                        timing["interval"],
                        timing["cadence"],
                        self.revisions[dev][1],
                    )
                    if self.retry_at.pop(dev, None) is not None:
                        self.errors.pop(dev, None)
                except (DeviceError, KeyError) as exc:
                    self.retry_at[dev] = time.monotonic() + 1
                    self.errors[dev] = str(exc)
            with self.service.lock:
                task = self.service.tasks.get(dev)
                frames = tuple(task.frames.values()) if task else ()
                period = max(
                    timing["interval"], (task.frame_interval or task.acquisition_seconds or 0) if task else 0
                )
                task_error = task.error if task else ""
            direct = self.ctl.recorded_frame(dev)
            if direct and direct.get("session_id") == session_id:
                batch = direct["batch"]
                source_request = direct.get("request")
                if source_request:
                    frames += (
                        MonitorFrame(
                            0,
                            direct["configuration_version"],
                            source_request,
                            batch,
                            direct["received_at"],
                            session_id,
                        ),
                    )
            manual = self.samples.get(dev)
            if manual:
                frames += (manual,)
            version = self.ctl.states[dev]["configurationVersion"]
            for row, spec in signals:
                compatible = sorted(
                    (
                        f
                        for f in frames
                        if f.session_id == session_id
                        and f.configuration_version == version
                        and (
                            f.request.kind == spec.source.kind
                            or (
                                spec.profile
                                and spec.profile.id == "phase_noise.at_offset"
                                and f.request.kind in ("trace", "timed_phase")
                            )
                        )
                        and (
                            any(m.channel == row.channel for m in f.batch.measurements)
                            if spec.profile
                            else select_sample(f.batch, row.channel, spec.source) is not None
                        )
                    ),
                    key=lambda f: f.received_at,
                    reverse=True,
                )
                self.status[row.id] = task_error or "等待兼容数据"
                if not compatible:
                    if dev in self.errors:
                        self.status[row.id] = self.errors[dev]
                    continue
                frame = compatible[0]
                if not spec.profile:
                    reading = scalar(frame, row.channel, spec.source)
                else:
                    reading, message = self.metric(dev, row, spec, frame)
                    if message:
                        self.status[row.id] = message
                if reading is None:
                    continue
                if (row.unit and row.unit != reading.unit) or (
                    row.quantity and row.quantity != reading.quantity
                ):
                    self.status[row.id] = "物理量或单位不兼容"
                    continue
                if not row.quantity or not row.unit:
                    self.learned[row.id] = (reading.quantity, reading.unit)
                self.values[row.id] = (dev, reading)
                age = time.perf_counter() - reading.received
                self.status[row.id] = (
                    "已过期" if age > max(5, period * 3) else bounds_status(reading.value, row)
                )
        if self.learned:
            items = [
                replace(
                    p,
                    rows=tuple(
                        replace(r, quantity=self.learned[r.id][0], unit=self.learned[r.id][1])
                        if r.id in self.learned
                        else r
                        for r in p.rows
                    ),
                )
                for p in self.presets.items
            ]
            self.presets.save(items)
            self.learned.clear()
            self.changed.emit()
        self.updated.emit()
        valid = {r.id for p in self.presets.items for r in p.rows}
        for store in (self.values, self.devices, self.status):
            for key in store.keys() - valid:
                del store[key]
        for key in list(self.metric_cache):
            if not any(
                r.channel == key[1] and r.profile == key[2] and self.devices.get(r.id, ("",))[0] == key[0]
                for r in active
            ):
                self.metric_cache.pop(key)
        for key in list(self.metric_jobs):
            if self.metric_jobs[key][1].done() and not any(
                r.channel == key[1]
                and r.profile == key[2]
                and json.dumps(r.parameters, sort_keys=True) == key[3]
                for r, _ in groups.get(key[0], ())
            ):
                self.metric_jobs.pop(key)

    def metric(self, dev, row, spec, frame):
        key = dev, row.channel, row.profile, json.dumps(row.parameters, sort_keys=True)
        token = frame.session_id, frame.configuration_version, frame.received_at
        pending = self.metric_jobs.get(key)
        if pending and pending[1].done():
            try:
                values, err = pending[1].result(), ""
            except Exception as exc:
                values, err = {}, str(exc)
            self.metric_cache[key] = pending[0], values, err
            del self.metric_jobs[key]
        cached = self.metric_cache.get(key)
        if (not cached or cached[0] != token) and key not in self.metric_jobs:
            window = None
            if spec.profile.supports_multiple_recordings:
                optical = spec.profile.id.startswith("optical_power.")
                if frame.sequence == 0:
                    history = self.ctl.recorded_window(dev, frame, optical)
                else:
                    history = (
                        self.service.power_window(dev) if optical else self.service.temperature_window(dev)
                    )
                if optical:
                    w = history
                    points = next((h.points for h in w.channels if h.channel == row.channel), ()) if w else ()
                    window = (tuple(p.watts for p in points), "W")
                else:
                    h = next((h for h in (history or ()) if h.channel == row.channel), None)
                    window = (
                        ()
                        if not h or h.count == 0
                        else (h.minimum,)
                        if h.count == 1
                        else (h.minimum, h.maximum),
                        h.unit if h else "",
                    )
            self.metric_jobs[key] = (
                token,
                self.worker.submit(compute_metric, spec, frame, row.channel, row.parameters, window),
            )
        if cached and cached[0][:2] == token[:2]:
            value = cached[1].get(row.metric)
            error = cached[2]
            if error.startswith(("等待完整", "等待兼容")):
                error = "等待兼容数据"
            return value, error or ("" if value else "有效样本不足")
        return None, "等待计算"

    def take_once(self, row):
        dev = self.resolve(row)
        if not dev or not self.ctl.has_connected_session(dev):
            raise DeviceError("仪器未连接")
        channels, specs = self.catalog(dev)
        spec = specs.get((row.source, row.profile, row.metric))
        if not spec or row.channel not in channels:
            raise DeviceError("通道或监视量不可用")
        request = spec.source.request((row.channel,), confirmed=True)
        sessions = self.ctl.runtime.sessions
        with self.service.lock, sessions.lock:
            task = self.service.tasks.get(dev)
            session = sessions.sessions[dev]
            if (
                (task and (task.running or task.stopping))
                or session.monitoring
                or session.controls_locked
                or session.state != "connected"
                or session.pending
                or session.control_group
                or session.control_pending
                or session.single_capture
                or session.closing
                or (session.stream_future and not session.stream_future.done())
                or dev in self.service.capture_saves
            ):
                raise DeviceError("仪器正在使用，等待兼容数据", "task_busy")
            sid, version = session.session_id, session.configuration_version
            if request.kind == "timed_phase":
                future = sessions.start_acquisition(dev, request, mode="general")
                self.ctl.watch(future)
                return
            if request.kind != "waveform":
                raise DeviceError("此监视量随仪器采集刷新")
            if self.catalogs[dev][0].details.get("single_acquisition"):
                future = sessions.submit_single(dev, SingleCapture(request))
            else:
                # Read one complete frame from the current input routing.
                request = replace(request, allow_setup=False)
                future = sessions.submit(dev, "acquire", request)

        def done(batch):
            actual = batch.configuration.get("_session_configuration_version", version)
            self.samples[dev] = MonitorFrame(0, actual, request, batch, session_id=sid)
            self.errors.pop(dev, None)
            self.tick()

        self.ctl.watch(future, done, lambda exc: self.errors.update({dev: str(exc)}))
