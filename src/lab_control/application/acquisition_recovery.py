"""Shared, cancellable recovery of acquisition connections; never replay controls."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Callable
from uuid import uuid4

from lab_control.domain.connections import network_host
from lab_control.domain.instruments import DeviceError, MeasurementBatch, ResourceCleanupError, utc_now
from lab_control.instruments.registry import requires_manual_connection

if TYPE_CHECKING:
    from lab_control.application.sessions import DeviceSession, SessionManager
    from lab_control.domain.connections import ConnectionConfig
    from lab_control.domain.instruments import DeviceIdentity
    from lab_control.storage.recording import Recording


def identify_for_connection(adapter, previous: DeviceIdentity | None) -> DeviceIdentity:
    observed = adapter.identify()
    if previous is None:
        return observed
    identity = observed
    if observed.confidence == getattr(adapter, "optional_firmware_evidence", None):
        identity = observed.with_firmware_history(previous)
    if (
        previous.last_known_firmware
        and identity.key == previous.key
        and identity.firmware_reference != previous.last_known_firmware
    ):
        if getattr(adapter, "identity", None) is observed:
            adapter.identity = previous
        raise DeviceError("设备固件与上次实读版本不一致或缺失", "qualification_stale")
    if identity is not observed:
        adapter.identity = identity
    return identity


RECOVERABLE_CODES = frozenset(
    {
        "timeout",
        "disconnected",
        "connection_failed",
        "device_not_present",
        "driver_missing",
        "resource_busy",
        "parse_error",
        "sdk_error",
        "acquisition_failed",
        "data_gap",
    }
)


def recoverable(error: BaseException) -> bool:
    return isinstance(error, DeviceError) and error.code in RECOVERABLE_CODES


BACKOFF = (0.2, 0.5, 1.0, 2.0, 5.0)
NETWORK_BACKOFF = (*BACKOFF, 15.0, 30.0, 60.0, 300.0)


def retry_delay(attempts: int, *, network: bool) -> float:
    schedule = NETWORK_BACKOFF if network else BACKOFF
    return schedule[min(max(attempts - 1, 0), len(schedule) - 1)]


def require_present(config: ConnectionConfig) -> None:
    from lab_control.discovery.providers import VendorSdkDiscoveryProvider
    from lab_control.domain.connections import canonical_resource

    resources = VendorSdkDiscoveryProvider((config,)).enumerate()
    if not any(
        canonical_resource(item.config.resource) == canonical_resource(config.resource) for item in resources
    ):
        raise DeviceError(f"SDK 枚举中未发现 {config.resource}", "device_not_present")


@dataclass
class Recovery:
    error: str
    code: str
    last_valid_at: str = ""
    id: str = field(default_factory=lambda: uuid4().hex)
    started_at: str = field(default_factory=utc_now)
    started: float = field(default_factory=time.monotonic)
    next_at: float = 0
    attempts: int = 0
    failed_reads: int = 1
    ready: bool = False
    cancelled: threading.Event = field(default_factory=threading.Event)
    network: bool = False
    discovered_resource: str = ""

    def failed(self, error: BaseException) -> None:
        self.error, self.code = str(error), getattr(error, "code", "device_error")
        self.ready = False
        # No queue of missed samples, no busy loop, no upper bound on outage duration.
        self.next_at = time.monotonic() + retry_delay(self.attempts, network=self.network)

    def snapshot(self) -> dict[str, Any]:
        return dict(
            id=self.id,
            started_at=self.started_at,
            elapsed_seconds=max(0, time.monotonic() - self.started),
            last_valid_at=self.last_valid_at,
            attempts=self.attempts,
            failed_reads=self.failed_reads,
            error=self.error,
            code=self.code,
            next_retry_seconds=max(0, self.next_at - time.monotonic()),
        )


class AcquisitionRecovery:
    def __init__(self, manager: SessionManager) -> None:
        self.manager = manager
        self.serial_route: Callable[[DeviceSession], ConnectionConfig] | None = None
        self.serial_verified: Callable[[DeviceSession, ConnectionConfig, DeviceIdentity], None] | None = None
        self.network_route: Callable[[DeviceSession], ConnectionConfig] | None = None
        self.verify_move: Callable[[DeviceSession, ConnectionConfig, DeviceIdentity], None] | None = None

    def event(
        self, session: DeviceSession, recovery: Recovery, event: str, recording: Recording | None = None
    ) -> None:
        details = recovery.snapshot()
        self.manager.audit.try_record(event, device_id=session.device_id, recovery=details)
        if recording is not None and not recording.error:
            recording.note_event(event, details)

    def _close_for_reconnect(self, session: DeviceSession) -> None:
        try:
            self.manager._close_adapter(session)
        except Exception as exc:
            # Only local handle cleanup may be retried before the next open.
            raise DeviceError(f"旧连接尚未释放：{exc}", "disconnected") from exc

    def reconnect(self, session: DeviceSession, recovery: Recovery) -> None:
        """One attempt on the existing device worker, keeping every resource lease."""
        if session.config.transport != "mock" and requires_manual_connection(session.config.adapter):
            raise DeviceError("请断开会话后重新手动选择串口", "manual_reconnect_required")
        manager = self.manager

        def check() -> None:
            if (
                recovery.cancelled.is_set()
                or session.closing
                or manager._closing
                or (session.active_task == "continuous_acquisition" and session.stop.is_set())
            ):
                raise DeviceError("自动重连已取消", "recovery_cancelled")

        check()
        original = session.identity
        if original is None:
            raise DeviceError("自动重连缺少原设备身份", "identity_conflict")
        recovery.attempts += 1
        recovery.network = bool(network_host(session.config.resource))
        with manager.lock:
            session.recovery = recovery
            session.state = "recovering"
            session.last_error = recovery.error
            session.settings.clear()
            session.frames.clear()
            session.last_status.clear()
            session.configuration_version += 1
            if session.capability_snapshot:
                session.capability_snapshot = replace(session.capability_snapshot, stale=True)
        manager.emit("recovery_started", session.device_id, recovery=recovery.snapshot())
        candidate = None
        config = session.config
        added_claims: set[str] = set()
        previous_claims: set[str] = set()
        try:
            # Teardown never sends LOCAL, reset or output commands. Any authorized
            # model setup runs only after the replacement identity is verified below.
            if session.cleanup_handle is not None:
                self._close_for_reconnect(session)
                manager.leases.release_resources(session.device_id, session.cleanup_claims)
                session.cleanup_claims.clear()
            reopen = getattr(session.adapter, "reopen_acquisition", None)
            if callable(reopen):
                # Keep model-owned measurement services alive across socket failures.
                candidate = session.adapter
                reopen()
            elif session.adapter is not None:
                manager._invalidate_acquisition(session, "reconnect")
                self._close_for_reconnect(session)
            check()
            if candidate is None:
                if recovery.network and self.network_route is not None:
                    config = self.network_route(session)
                elif self.serial_route is not None:
                    config = self.serial_route(session)
                if config != session.config:
                    from lab_control.application.leases import connection_claims

                    old = manager.leases.keys(connection_claims(session.config))
                    new = manager.leases.keys(connection_claims(config))
                    manager.leases.acquire(session.device_id, connection_claims(config), identified=True)
                    added_claims, previous_claims = new - old, old - new
                if recovery.code == "device_not_present":
                    require_present(config)
                candidate = manager.factory(config)
            check()
            identity = identify_for_connection(candidate, original)
            same_model = (identity.manufacturer.strip().casefold(), identity.model.strip().casefold()) == (
                original.manufacturer.strip().casefold(),
                original.model.strip().casefold(),
            )
            if not same_model or (original.serial and identity.serial != original.serial):
                raise DeviceError("重连设备身份与原采集设备不一致，已停止恢复", "identity_conflict")
            if (
                session.config.expected_serial
                and identity.serial
                and identity.serial != session.config.expected_serial
            ):
                raise DeviceError("重连设备序列号与配置不一致", "identity_conflict")
            if original.firmware_reference and identity.firmware_reference != original.firmware_reference:
                raise DeviceError("重连设备固件与原采集会话不一致", "qualification_stale")
            if recovery.network and config.resource != session.config.resource and self.verify_move:
                self.verify_move(session, config, identity)
            check()
            bind = getattr(candidate, "bind_control_state", None)
            if callable(bind):
                key = f"device-control-state/{session.device_id}"
                bind(
                    lambda: manager.repository.load_workspace(key),
                    lambda value: manager.repository.save_workspace(key, value),
                )
            prepare = getattr(candidate, "prepare_acquisition", None)
            if session.acquisition_setup_enabled and callable(prepare):
                # Only the model's declared, already-authorized acquisition setup may run.
                # Its failure may follow an uncertain write: never automatically repeat it.
                try:
                    prepare()
                except Exception as exc:
                    raise DeviceError(
                        f"重连采集初始化未确认：{exc}", "recovery_initialization_failed"
                    ) from exc
            check()
            if config != session.config:
                if self.serial_verified is not None:
                    self.serial_verified(session, config, identity)
                manager.leases.release_resources(session.device_id, previous_claims)
                session.config = config
                added_claims.clear()
            session.adapter, candidate = candidate, None
            session.resource_state = "held"
            session.identity = identity
            manager._refresh_capabilities(session)
            check()
            with manager.lock:
                session.state = (
                    "busy"
                    if session.monitoring or session.active_task == "continuous_acquisition"
                    else "connected"
                )
                session.last_error = ""
                session.connection_error_code = ""
                recovery.ready = True
            manager.emit("reconnected", session.device_id, recovery=recovery.snapshot())
        except Exception as exc:
            if isinstance(exc, ResourceCleanupError):
                session.cleanup_handle = exc.resource
                session.resource_state = "release_unconfirmed"
            if candidate is not None and candidate is not session.adapter:
                try:
                    candidate.close()
                except Exception as cleanup:
                    session.cleanup_handle = candidate
                    session.resource_state = "release_unconfirmed"
                    manager.audit.try_record(
                        "recovery_cleanup_failed", device_id=session.device_id, error=str(cleanup)
                    )
            if added_claims:
                if session.cleanup_handle is not None:
                    session.cleanup_claims.update(added_claims)
                else:
                    manager.leases.release_resources(session.device_id, added_claims)
            with manager.lock:
                session.state = "recovering"
            if isinstance(exc, ResourceCleanupError) and getattr(exc.primary, "code", "") in {
                "timeout",
                "disconnected",
                "connection_failed",
                "device_not_present",
            }:
                raise DeviceError(str(exc), "disconnected") from exc
            raise

    def complete(
        self,
        session: DeviceSession,
        recovery: Recovery,
        batch: MeasurementBatch,
        recording: Recording | None = None,
    ) -> MeasurementBatch:
        details = recovery.snapshot() | {"resumed_at": batch.timestamp}
        self.event(session, recovery, "acquisition_resumed", recording)
        with self.manager.lock:
            session.recovery = None
            session.last_error = ""
        return replace(batch, configuration={**batch.configuration, "_acquisition_gap": details})

    def wait(self, session: DeviceSession, recovery: Recovery) -> bool:
        """Direct recordings use their worker; a stop interrupts every backoff."""
        while not session.stop.is_set() and not self.manager._closing:
            remaining = recovery.next_at - time.monotonic()
            if remaining > 0:
                session.stop.wait(min(remaining, 0.5))
                continue
            try:
                self.reconnect(session, recovery)
                return True
            except DeviceError as exc:
                if exc.code == "recovery_cancelled":
                    break
                if not recoverable(exc):
                    raise
                recovery.failed(exc)
                self.event(session, recovery, "reconnect_failed")
        recovery.cancelled.set()
        return False
