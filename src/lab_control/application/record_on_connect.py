"""Optional per-installation recording of scalar scan devices after identity verification."""

import json

from lab_control.domain.instruments import AcquisitionRequest, DeviceError


def load_devices(repository):
    path = repository.root / "config" / "record-on-connect.json"
    try:
        devices = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(devices, dict) or not all(
            isinstance(key, str) and isinstance(adapter, str) and adapter for key, adapter in devices.items()
        ):
            raise ValueError("Expected an object mapping device IDs to adapter IDs")
        return devices
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        repository.audit("record_on_connect_config_error", error=str(exc))
        return {}


def start_recording(runtime, device_id, adapter):
    if runtime.record_on_connect.get(device_id) != adapter:
        return
    session = runtime.sessions.sessions.get(device_id)
    if session is None or session.closing:
        return

    def failed(exc):
        message = f"已连接，自动记录启动失败：{exc}"
        with runtime.sessions.lock:
            if runtime.sessions.sessions.get(device_id) is session:
                session.last_error = message
        runtime.sessions.audit.try_record("record_on_connect_failed", device_id=device_id, error=message)
        runtime.sessions.emit("error", device_id, error=message)

    def finished(future):
        try:
            future.result()
        except Exception as exc:
            failed(exc)

    try:
        state = runtime.monitoring.snapshots().get(device_id, {})
        if state.get("recording"):
            return
        if state.get("running"):
            future = runtime.monitoring.set_recording(device_id, True, background=True)
        else:
            caps = runtime.sessions.capability_snapshot(device_id)
            channels = tuple(caps.details.get("acquisition_channels", ())) if caps else ()
            if not channels or caps.stale:
                raise DeviceError("没有已核验的采集通道", "acquisition_unavailable")
            timing = runtime.repository.load_workspace(f"monitor-timing/general/{device_id}", {})
            future = runtime.monitoring.start(
                device_id,
                (AcquisitionRequest(channels=channels, confirmed=True, allow_setup=False),),
                interval=timing.get("interval", 0.5),
                cadence=timing.get("cadence", "interval"),
                record_interval=timing.get("record_interval"),
                csv_interval=timing.get("csv_interval", 5),
                recording=True,
                background=True,
            )
        if future is not None:
            future.add_done_callback(finished)
    except Exception as exc:
        failed(exc)
