"""Desktop connection requests and cancellable, per-asset retry scheduling."""

from __future__ import annotations

import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import TYPE_CHECKING

from lab_control.application.acquisition_recovery import require_present, retry_delay
from lab_control.domain.connections import network_host
from lab_control.domain.instruments import DeviceError
from lab_control.instruments.registry import requires_manual_connection

if TYPE_CHECKING:
    from lab_control.application.runtime import ApplicationRuntime

TRANSIENT = {"timeout", "disconnected", "connection_failed", "device_not_present"}
DEFER = {"scan_busy", "maintenance_busy", "connection_batch_busy", "task_busy"}
LABELS = {
    "waiting": "等待设备接入",
    "retrying": "正在重连",
    "blocked": "需要处理",
    "cleanup": "正在释放连接",
}


@dataclass
class Intent:
    device_id: str
    configure: bool | None = True
    batch: threading.Event | None = None
    phase: str = "opening"
    attempts: int = 0
    retries: int = 0
    terminal_error: tuple[str, str] | None = None
    last_recovery: str = ""
    # Pin identity across scans while the device is absent.
    expected: tuple[str, str, str, str] | None = None
    due: float | None = None
    error: str = ""
    code: str = ""
    future: Future | None = None
    disconnecting: bool = False
    cleanup_only: bool = False
    awaiting_presence: bool = False
    serial_detached: bool = False
    resource: str = ""
    discovered_resource: str = ""


class ConnectionRetry:
    def __init__(self, runtime: ApplicationRuntime):
        self.runtime = runtime
        self.condition = threading.Condition()
        self.intents: dict[str, Intent] = {}
        self.disconnected: set[str] = set()
        self.closed = False
        self.workers = ThreadPoolExecutor(max_workers=4, thread_name_prefix="connection-retry")
        self.thread = threading.Thread(target=self._schedule, name="connection-timer", daemon=True)
        self.unsubscribe = runtime.sessions.subscribe(self._session_event)
        self.thread.start()

    def _session_event(self, event):
        if event["event"] != "disconnected" or event.get("cleanupOnly"):
            return
        key = event["device_id"]
        with self.condition:
            item = self.intents.get(key)
            if item and not item.disconnecting:
                self.disconnected.add(key)
                self.intents.pop(key)
                self.condition.notify_all()

    def snapshots(self):
        with self.condition:
            return {
                key: {
                    "phase": item.phase,
                    "label": LABELS.get(item.phase, ""),
                    "attempts": item.retries,
                    "error": item.error,
                    "code": item.code,
                    "lastRecovery": item.last_recovery,
                    "inFlight": bool(item.future and not item.future.done()),
                }
                for key, item in self.intents.items()
            }

    def _mark_managed(self, key):
        sessions = self.runtime.sessions
        with sessions.lock:
            session = sessions.sessions.get(key)
            if session:
                session.connection_managed = True

    def adopt(self, key):
        with self.condition:
            if key not in self.disconnected:
                session = self.runtime.sessions.sessions.get(key)
                self.intents.setdefault(
                    key,
                    Intent(
                        key, configure=bool(session and session.acquisition_setup_enabled), phase="online"
                    ),
                )
        self._mark_managed(key)

    def can_retry(self, key, state):
        with self.condition:
            item = self.intents.get(key)
            if item and (item.phase == "blocked" or item.disconnecting):
                return False
            if item and item.phase in {"waiting", "retrying", "opening"}:
                return True
        return state.get("connectionErrorCode") in TRANSIENT | {"cleanup_failed"}

    def suppressed_devices(self):
        with self.condition:
            return frozenset(self.disconnected)

    def owns(self, key):
        with self.condition:
            item = self.intents.get(key)
            return key in self.disconnected or bool(
                item and not (item.serial_detached and item.phase == "waiting")
            )

    def serial_removed(self, key):
        """Release the old port, so serial discovery can verify a replacement route."""
        with self.condition:
            item = self.intents.get(key)
            if (
                not item
                or item.disconnecting
                or item.phase in {"blocked", "opening"}
                or (item.future and not item.future.done())
            ):
                return
            item.serial_detached = item.cleanup_only = item.disconnecting = True
            item.due, item.phase = None, "cleanup"
            item.future = self.workers.submit(self._disconnect, item)
            return item.future

    def request(self, key, *, batch=None, configure=True, reconnect=False, hotplug=False):
        with self.condition:
            if self.closed or self.runtime.discovery.closing:
                raise DeviceError("程序正在退出", "runtime_closing")
            if hotplug and key in self.disconnected:
                raise DeviceError("设备已手动断开", "recovery_cancelled")
            if batch and batch.is_set():
                raise DeviceError("已停止批量连接", "recovery_cancelled")
            item = self.intents.get(key)
            if item and item.disconnecting:
                raise DeviceError("连接正在释放", "task_busy")
            self.disconnected.discard(key)
            if item is None:
                item = self.intents[key] = Intent(key, configure, batch)
            elif batch is None and not hotplug:
                item.batch = None  # An independent request outlives a cancelled batch.
            if item.future and not item.future.done():
                return item.future
            if item.serial_detached:
                if not hotplug:
                    raise DeviceError("等待串口重新接入并核验身份", "device_not_present")
                item.serial_detached = False
            if (
                item.phase == "blocked"
                and (batch or hotplug)
                and item.code not in {"resource_busy", "driver_missing"}
            ):
                raise DeviceError(item.error, item.code)
            if reconnect:
                item.phase = "retrying"
            return self._submit(item, batch=batch, reconnect=reconnect)

    def _submit(self, item: Intent, *, batch=None, reconnect=False):
        if item.phase in {"retrying", "waiting"}:
            item.retries += 1
        item.due = None
        item.future = self.workers.submit(self._attempt, item, batch, reconnect)
        return item.future

    def _defer(self, item: Intent, phase: str, error: Exception) -> None:
        item.phase = phase
        item.code = getattr(error, "code", "device_error")
        item.error = str(error)
        network = bool(network_host(item.resource)) and getattr(error, "code", "") in TRANSIENT
        item.due = time.monotonic() + retry_delay(item.attempts, network=network)
        self.condition.notify_all()

    def _schedule(self):
        with self.condition:
            while not self.closed:
                due = [(item.due, item) for item in self.intents.values() if item.due is not None]
                if not due:
                    self.condition.wait()
                    continue
                deadline, item = min(due, key=lambda pair: pair[0])
                delay = deadline - time.monotonic()
                if delay > 0:
                    self.condition.wait(delay)
                elif item.disconnecting:
                    item.retries += 1
                    item.due = None
                    item.future = self.workers.submit(self._disconnect, item)
                else:
                    self._submit(item)

    def _state(self, key):
        return next((s for s in self.runtime.sessions.snapshots() if s["stableDeviceId"] == key), {})

    def _check(self, item: Intent) -> None:
        with self.condition:
            if (
                self.closed
                or item.disconnecting
                or self.intents.get(item.device_id) is not item
                or self.runtime.discovery.closing
            ):
                raise DeviceError("重连已取消", "recovery_cancelled")

    def _open(self, item: Intent, batch: threading.Event | None, reconnect: bool):
        app = self.runtime
        key = item.device_id
        state = self._state(key)
        if state:
            item.resource = state["resource"]
        if self.runtime.hardware and requires_manual_connection(state.get("adapterId", "")):
            raise DeviceError("请手动选择设备串口", "manual_reconnect_required")
        if (
            state.get("monitoring")
            or state.get("recording")
            or state.get("activeTask") == "continuous_acquisition"
        ):
            if reconnect:
                raise DeviceError("设备任务进行中", "task_busy")
            return app.sessions.sessions[key].identity
        if state:
            if (
                state["connectionState"] in {"connected", "busy"}
                and not reconnect
                and item.phase in {"opening", "online"}
            ):
                return app.sessions.sessions[key].identity
            if (
                state.get("pending")
                or state.get("activeTask")
                or state.get("controlGroup")
                or state.get("controlPending")
                or state.get("controlsLocked")
            ):
                raise DeviceError("等待设备当前操作完成", "task_busy")
        if item.awaiting_presence:
            from lab_control.application.config import config_from_record

            session = app.sessions.sessions.get(key)
            config = (
                session.config
                if session
                else config_from_record(next(r for r in app.inventory.all() if r["id"] == key))
            )
            require_present(config)
            item.awaiting_presence = False
        self._check(item)
        if state and state.get("identity") and state["resourceReleaseState"] != "released":
            with app.discovery.lock:
                if not batch:
                    app._check_connection_admission()
                if app.discovery.future and not app.discovery.future.done():
                    raise DeviceError("等待设备扫描完成", "scan_busy")
                future = app.sessions.reconnect(key, allow_connected=True)
            return future.result()
        if state:
            # A failed initial open has no verified session identity to resume.
            try:
                app.sessions.disconnect(key, drain_reads=True, cleanup_only=True)
            except Exception as exc:
                if self._state(key).get("resourceReleaseState") == "release_unconfirmed":
                    raise DeviceError(f"旧连接尚未释放：{exc}", "disconnected") from exc
                raise
        self._check(item)
        record = next(r for r in app.inventory.all() if r["id"] == key)
        item.resource = record["config"]["resource"]
        if item.attempts and network_host(item.resource) and app.hardware:
            app.network_recovery.refresh_unconnected(record)
        if item.expected is None:
            known = record.get("identity") or {}
            item.expected = (
                known.get("manufacturer", ""),
                known.get("model", ""),
                known.get("serial", ""),
                known.get("firmware") or known.get("last_known_firmware", ""),
            )

        def verify(identity):
            self._check(item)
            if identity is None:
                return
            assert item.expected is not None
            maker, model, serial, firmware = item.expected
            if (
                (maker and maker.casefold() != identity.manufacturer.casefold())
                or (model and model.casefold() != identity.model.casefold())
                or (serial and serial != identity.serial)
            ):
                raise DeviceError("设备身份与原连接不一致", "identity_conflict")
            if firmware and identity.firmware_reference != firmware:
                raise DeviceError("设备固件与原连接不一致", "qualification_stale")

        connect = app._connect if batch else app.connect
        return connect(key, configure_acquisition=item.configure, admission=verify).result()

    def _attempt(self, item: Intent, batch: threading.Event | None, reconnect: bool):
        key = item.device_id
        self._check(item)
        try:
            identity = self._open(item, batch, reconnect)
            state = self._state(key)
            if state.get("connectionState") == "degraded" and state.get("connectionErrorCode") in TRANSIENT:
                raise DeviceError(state["lastError"], state["connectionErrorCode"])
            self._check(item)
            self._mark_managed(key)
        except Exception as exc:
            code = getattr(exc, "code", "device_error")
            state = self._state(key)
            if state.get("connectionErrorCode") == "recovery_initialization_failed":
                code = "recovery_initialization_failed"
            if code == "cleanup_failed" and state.get("resourceReleaseState") == "release_unconfirmed":
                code = getattr(getattr(exc, "primary", None), "code", "cleanup_failed")
            with self.condition:
                if item.disconnecting or self.closed or self.intents.get(key) is not item:
                    raise
                item.attempts += 1
                if item.batch and item.batch.is_set():
                    item.disconnecting, item.cleanup_only, item.phase = True, True, "cleanup"
                    item.due = time.monotonic()
                    self.condition.notify_all()
                elif code in TRANSIENT or code in DEFER:
                    if code == "device_not_present":
                        item.awaiting_presence = True
                    phase = "waiting" if item.awaiting_presence else "retrying"
                    self._defer(item, phase, exc)
                else:
                    item.phase, item.error, item.code = "blocked", str(exc), code
                    if state.get("resourceReleaseState") == "release_unconfirmed":
                        item.terminal_error = (str(exc), code)
                        item.disconnecting = item.cleanup_only = True
                        self._defer(item, "cleanup", exc)
            raise
        with self.condition:
            if not item.disconnecting:
                item.awaiting_presence = False
                if item.error and item.retries:
                    item.last_recovery = f"最近一次连接恢复：重试 {item.retries} 次；{item.error}"
                item.retries = 0
                item.phase, item.attempts, item.error, item.code = "online", 0, "", ""
                item.batch = None
        return identity

    def discovered(self, report):
        if report["state"] != "complete":
            return
        desktop = getattr(self.runtime, "device_changes", None)
        auto = desktop is not None and desktop.enabled and not desktop.stop.is_set()
        if auto:
            desktop.resolved(report)
        records = {r["id"]: r for r in self.runtime.inventory.all()} if auto else {}
        explicit_retry = report.get("scope") == "address_retry"
        for result in report["items"]:
            if auto:
                key = result.get("device_id")
                if not key:
                    continue
                row = records.get(key, {})
                known = row.get("identityKey") and (
                    result["reason"] == "identified"
                    or (explicit_retry and result["reason"] == "cached_session_unconfirmed")
                )
                descriptor = row.get("candidates", {}).get(result["resource"], {})
                sdk = (
                    any(name.startswith("sdk:") for name in descriptor.get("sources", {}))
                    and result["reason"] == "sdk_identity_requires_explicit_connection"
                    and (row.get("config", {}).get("expected_serial") or row.get("identityKey"))
                )
                if (
                    (known or sdk)
                    and row.get("adapterId")
                    and not row.get("identityConflict")
                    and not requires_manual_connection(row["adapterId"])
                    and (
                        (
                            explicit_retry
                            and self._state(key).get("connectionState")
                            not in {"connected", "busy", "connecting"}
                        )
                        or (not self.owns(key) and not self._state(key))
                    )
                ):
                    try:
                        self.request(key, configure=None, hotplug=not explicit_retry)
                    except DeviceError as exc:
                        if exc.code not in {"recovery_cancelled", "runtime_closing"}:
                            self.runtime.repository.audit(
                                "auto_connect_deferred", device_id=key, error=str(exc)
                            )
            resource = result["resource"]
            if result["reason"] != "identified" or not network_host(resource):
                continue
            key = result["device_id"]
            with self.condition:
                item = self.intents.get(key)
                if (
                    item
                    and item.phase in {"waiting", "retrying"}
                    and item.due is not None
                    and resource not in (item.resource, item.discovered_resource)
                ):
                    item.discovered_resource = resource
                    item.due = time.monotonic()
                    self.condition.notify_all()
            with self.runtime.sessions.lock:
                session = self.runtime.sessions.sessions.get(key)
                recovery = session.recovery if session else None
                if recovery and resource not in (session.config.resource, recovery.discovered_resource):
                    recovery.discovered_resource = resource
                    recovery.next_at = time.monotonic()
                    self.runtime.monitoring.wake.set()

    def settings_failed(self, key, session_id, exc):
        sessions = self.runtime.sessions
        with self.condition, sessions.lock:
            session = sessions.sessions.get(key)
            if (
                not session
                or session.session_id != session_id
                or session.monitoring
                or session.recording
                or session.control_pending
                or session.control_group
                or session.active_task
            ):
                return
            item = self.intents.get(key)
            if self.closed or not item or item.phase != "online" or item.disconnecting:
                return
            session.state = "degraded"
            session.connection_error_code = exc.code
            session.last_error = str(exc)
            self._defer(item, "retrying", exc)

    def disconnect(self, key):
        state = self._state(key)
        with self.condition:
            item = self.intents.get(key)
            if item:
                item.serial_detached = False
            if item and item.disconnecting:
                item.terminal_error = None
                self.disconnected.add(key)
                return item.future
            if (
                state.get("monitoring")
                or state.get("recording")
                or state.get("controlPending")
                or state.get("controlGroup")
                or state.get("activeTask") == "continuous_acquisition"
            ):
                raise DeviceError("请等待当前操作结束后断开", "task_busy")
            self.disconnected.add(key)
            if item is None:
                item = self.intents[key] = Intent(key)
            item.cleanup_only = (
                item.phase in {"waiting", "retrying", "blocked"}
                or state.get("connectionState") in {"error", "degraded"}
                or state.get("resourceReleaseState") == "release_unconfirmed"
            )
            item.disconnecting, item.due, item.phase = True, None, "cleanup"
            session = self.runtime.sessions.sessions.get(key)
            if session and session.active_task == "reconnect" and session.recovery:
                session.recovery.cancelled.set()
            previous = item.future
            item.future = self.workers.submit(self._disconnect, item, previous)
            self.condition.notify_all()
            return item.future

    def _disconnect(self, item: Intent, previous=None):
        if previous:
            try:
                previous.result()
            except Exception:
                pass
        key = item.device_id
        try:
            if self._state(key):
                self.runtime.sessions.disconnect(key, drain_reads=True, cleanup_only=item.cleanup_only)
        except Exception as exc:
            state = self._state(key)
            if state.get("resourceReleaseState") == "release_unconfirmed":
                with self.condition:
                    item.cleanup_only = True
                    item.attempts += 1
                    self._defer(item, "cleanup", exc)
                raise
            with self.condition:
                if state:
                    item.phase, item.disconnecting = "online", False
                    self.disconnected.discard(key)
                else:
                    self.intents.pop(key, None)
            raise
        with self.condition:
            if item.serial_detached:
                item.phase, item.disconnecting = "waiting", False
                item.error, item.code = "等待串口重新接入并核验身份", "disconnected"
            elif item.terminal_error:
                item.error, item.code = item.terminal_error
                item.terminal_error = None
                item.phase, item.disconnecting = "blocked", False
            else:
                self.intents.pop(key, None)

    def cancel_batch(self, batch):
        with self.condition:
            for key, item in tuple(self.intents.items()):
                if item.batch is not batch or item.disconnecting:
                    continue
                if item.phase in {"waiting", "retrying"}:
                    item.disconnecting, item.cleanup_only, item.phase = True, True, "cleanup"
                    item.due = None
                    item.future = self.workers.submit(self._disconnect, item, item.future)
            self.condition.notify_all()

    def close(self):
        with self.condition:
            self.closed = True
            self.condition.notify_all()
        self.thread.join()
        self.workers.shutdown(wait=True)
        self.unsubscribe()
