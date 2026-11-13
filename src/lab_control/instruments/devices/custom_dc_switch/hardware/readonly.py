"""Verify a selected bridge and the normal session lifetime using only the firmware identity query."""

import json
import re
from pathlib import Path
from typing import Any

from lab_control.application.runtime import ApplicationRuntime
from lab_control.discovery.providers import SerialDiscoveryProvider
from lab_control.domain.connections import ConnectionConfig
from lab_control.domain.instruments import DeviceError, jsonable, utc_now
from lab_control.storage.hardware_documents import refresh_after_batch
from lab_control.storage.hardware_records import atomic_json, digest, publish_report
from lab_control.storage.repository import application_home
from lab_control.transport.visa import VisaRuntime

from .. import DEFINITION
from ..adapter import ADAPTER_ID, MODEL, SwitchBoardAdapter, SwitchWire
from .records import summarize


class IdentityTrace:
    def __init__(self, wire: SwitchWire, transcript: Path):
        self.wire, self.transcript = wire, transcript

    def log(self, direction: str, raw: bytes) -> None:
        with self.transcript.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"at": utc_now(), "direction": direction, "hex": raw.hex(" ")}) + "\n")

    def send(self, payload: bytes) -> None:
        if payload != b"*IDN?\n":
            raise DeviceError("只读验收只允许身份查询", "unsupported")
        self.wire.send(payload)
        self.log("tx", payload)

    def receive(self, *, timeout: float = 0.0) -> bytes:
        raw = self.wire.receive(timeout=timeout)
        if raw:
            self.log("rx", raw)
        return raw

    def close(self) -> None:
        self.wire.close()


def verify(
    config: ConnectionConfig,
    folder: Path,
    *,
    duration: float,
    root: Path | None,
    input_condition: str,
) -> dict[str, Any]:
    hardware_root = (root or application_home()).resolve() / "hardware-tests"
    folder = folder.resolve()
    if folder.parent.parent != hardware_root:
        raise ValueError("请选择运行目录下的 hardware-tests/<人工资产号>/<批次>")
    folder.mkdir(parents=True, exist_ok=False)
    transcript = folder / "communication.jsonl"
    transcript.touch()
    stages = {
        name: {"state": "not_tested"}
        for name in (
            "enumeration",
            "identity",
            "manual_binding",
            "manual_rebinding",
            "compatibility_connection",
            "parameter_read",
            "parameter_write",
            "restoration",
            "reconnection",
            "physical_output",
            "disconnect",
        )
    }
    stages["parameter_read"]["reason"] = "协议无输出状态查询"
    report: dict[str, Any] = {
        "adapter_id": ADAPTER_ID,
        "model": MODEL,
        "resource": config.resource,
        "started_at": utc_now(),
        "input_condition": input_condition,
        "scope": "枚举 CP210x，连接与重连均只发送 *IDN?，核对固件型号",
        "source_sha256": {path.name: digest(path) for path in DEFINITION.root.glob("*.py")},
        "stages": stages,
        "checks_passed": False,
        "final_state": "板端输出状态未知",
    }
    visa = VisaRuntime()
    stage = "enumeration"
    try:
        port = re.fullmatch(r"(?:COM([1-9][0-9]*)|ASRL([1-9][0-9]*)::INSTR)", config.resource, re.I)
        if config.adapter != ADAPTER_ID or config.transport != "visa" or port is None:
            raise DeviceError("请选择一个 Windows COM 口及开关板适配器", "invalid_parameter")
        selected = "COM" + (port[1] or port[2])
        ports = [p for p in SerialDiscoveryProvider().enumerate() if p.config.resource == selected]
        atomic_json(folder / "enumeration.json", jsonable(ports))
        if len(ports) != 1 or not DEFINITION.matches_manual_port(ports[0].descriptor):
            raise DeviceError("所选口未枚举为 CP210x", "driver_missing")
        stages[stage] = {"state": "passed"}
        stage = "manual_binding"
        with ApplicationRuntime(root=root, visa=visa) as runtime:
            factory = runtime.sessions.factory

            def traced(connection: ConnectionConfig):
                adapter = factory(connection)
                assert isinstance(adapter, SwitchBoardAdapter)
                adapter.transport = IdentityTrace(adapter.transport, transcript)
                return adapter

            runtime.sessions.factory = traced
            for stage in ("manual_binding", "manual_rebinding"):
                device_id, opened = runtime.connect_manual_serial(ADAPTER_ID, selected)
                identity = opened.result(timeout=duration + 10)
                report["identity"] = jsonable(identity)
                stages["identity"] = {"state": "passed", "reason": identity.raw}
                stages["compatibility_connection" if stage == "manual_binding" else "reconnection"] = {
                    "state": "passed"
                }
                status = runtime.sessions.submit(device_id, "status").result(timeout=duration + 10)
                stages[stage] = {
                    "state": "passed",
                    "reason": "固件已通过身份核验",
                    "status": jsonable(status),
                }
                stage = "disconnect"
                runtime.sessions.disconnect(device_id)
                stages[stage] = {"state": "passed"}
        report["checks_passed"] = True
    except Exception as exc:
        code = getattr(exc, "code", "")
        stages[stage] = {
            "state": "blocked"
            if code in {"invalid_parameter", "driver_missing", "resource_busy", "process_busy"}
            else "failed",
            "reason": str(exc),
        }
    finally:
        report["finished_at"] = utc_now()
        atomic_json(folder / "results.json", report)
        publish_report(folder, summarize)
        refresh_after_batch(folder)
    return report
