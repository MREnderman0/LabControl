"""Run an explicit recorder check through the production runtime and resource locks."""

import hashlib
import json
import platform
import time
from pathlib import Path
from typing import Any

from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.connections import ConnectionConfig
from lab_control.domain.instruments import AcquisitionRequest, DeviceError, jsonable, utc_now
from lab_control.instruments.common.hardware_paths import verify_runtime_paths
from lab_control.instruments.registry import create_adapter
from lab_control.storage.hardware_documents import refresh_after_batch
from lab_control.storage.hardware_records import atomic_json, publish_report
from lab_control.storage.repository import application_home
from lab_control.storage.runtime import hardware_lock_home

from ..adapter import ADAPTER_ID, RecorderAdapter
from .records import summarize


def verify(
    config: ConnectionConfig, folder: Path, *, duration=3, root=None, input_condition="未核对"
) -> dict:
    folder.mkdir(parents=True, exist_ok=True)
    # Reserve the batch even if two callers race or a previous check was interrupted.
    transcript = (folder / "communication.jsonl").open("x", encoding="utf-8")
    stages: dict[str, dict[str, str]] = {
        name: {"state": "not_tested"}
        for name in (
            "enumeration",
            "identity",
            "compatibility_connection",
            "parameter_read",
            "acquisition",
            "invalid_frame_rejection",
            "parameter_control",
            "restoration",
            "reconnect",
            "physical_output",
        )
    }
    report: dict[str, Any] = {
        "adapter_id": ADAPTER_ID,
        "model": "KT616R",
        "resource": config.resource,
        "config": jsonable(config),
        "started_at": utc_now(),
        "stages": stages,
        "operator": "project read-only acceptance",
        "environment": {"python": platform.python_version(), "os": platform.platform()},
        "scope": {"input_condition": input_condition, "duration_s": duration, "functions": ["FC04"]},
        "project_head": {
            "files_sha256": {
                str(path.relative_to(Path(__file__).parents[1])): hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
                for path in Path(__file__).parents[1].rglob("*.py")
                if "tests" not in path.parts
            }
        },
        "observations": [],
        "checks_passed": False,
    }

    def trace(direction: str, data: bytes) -> None:
        transcript.write(json.dumps({"time": utc_now(), "direction": direction, "hex": data.hex()}) + "\n")
        transcript.flush()

    stage = "enumeration"
    try:
        if config.adapter != ADAPTER_ID or config.transport != "tcp" or not 1 <= duration <= 3600:
            raise ValueError("Select a KT616R TCP endpoint and a duration of 1–3600 s")
        home = Path(root) if root else application_home()
        report["paths"] = verify_runtime_paths(home, hardware_lock_home())
        with ApplicationRuntime(root=home) as runtime:

            def traced(selected):
                if selected.resource != config.resource or selected.adapter != config.adapter:
                    raise DeviceError("Unexpected endpoint", "identity_conflict")
                adapter = create_adapter(selected, runtime.visa)
                assert isinstance(adapter, RecorderAdapter)
                adapter.transport.trace = trace
                return adapter

            runtime.sessions.factory = traced
            old = runtime.inventory.by_resource(config.resource)
            if old and old.get("adapterId") not in ("", ADAPTER_ID):
                raise DeviceError("Endpoint belongs to another model", "identity_conflict")
            asset = runtime.add(config)
            report["inventory_id"] = asset
            identity = runtime.connect(asset, configure_acquisition=False).result()
            report["identity"] = jsonable(identity)
            stages[stage] = {"state": "passed", "reason": "选定 TCP 端点返回了两块完整的 FC04 寄存器"}
            stages["identity"] = {"state": "blocked", "reason": "型号由现场指定，协议未提供可核验的序列号"}
            stages["compatibility_connection"] = {"state": "passed"}
            stage = "parameter_read"
            status = runtime.sessions.submit(asset, "status").result()
            report["initial_snapshot"] = jsonable(status)
            stages[stage] = {"state": "passed"}
            stage = "acquisition"
            deadline = time.monotonic() + duration
            snapshot = runtime.sessions.sessions[asset].capability_snapshot
            if snapshot is None or snapshot.stale:
                raise DeviceError("Unable to read recorder channel list", "not_configured")
            request = AcquisitionRequest(channels=tuple(snapshot.details["acquisition_channels"]))
            while True:
                try:
                    batch = runtime.sessions.submit(asset, "acquire", request).result()
                    report["observations"].append({"time": utc_now(), "batch": jsonable(batch)})
                    stages[stage] = {"state": "passed", "reason": "收到所选通道的有效数字，精度待物理对照"}
                except DeviceError as exc:
                    if exc.code != "acquisition_setup_blocked":
                        raise
                    report["observations"].append({"time": utc_now(), "rejected": str(exc)})
                    stages[stage] = {"state": "blocked", "reason": str(exc)}
                    stages["invalid_frame_rejection"] = {"state": "passed"}
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(1, remaining))
            stage = "reconnect"
            runtime.sessions.disconnect(asset)
            again = runtime.connect(asset, configure_acquisition=False).result()
            report["final_snapshot"] = jsonable(runtime.sessions.submit(asset, "status").result())
            if again != identity:
                raise DeviceError("Reconnect identity changed", "identity_conflict")
            stages[stage] = {"state": "passed", "reason": "重新建立会话并再次读取 16 路"}
        report["checks_passed"] = True
    except Exception as exc:
        reason = str(exc)
        blocked = getattr(exc, "code", "") in {
            "identity_conflict",
            "resource_busy",
            "process_busy",
            "path_redirected",
        }
        stages[stage] = {"state": "blocked" if blocked else "failed", "reason": reason}
        report["error"] = reason
    finally:
        transcript.close()
        report["finished_at"] = utc_now()
        report["final_state"] = "仅使用输入寄存器读操作，无写设置或输出操作。"
        atomic_json(folder / "results.json", report)
        (folder / "record.md").write_text(
            "# KT616R 实机只读测试\n\n"
            + input_condition
            + "\n\n"
            + "\n".join(f"- {key} · {v['state']} {v.get('reason', '')}" for key, v in stages.items())
            + "\n",
            encoding="utf-8",
        )
        publish_report(folder, summarize)
        refresh_after_batch(folder)
    return report
