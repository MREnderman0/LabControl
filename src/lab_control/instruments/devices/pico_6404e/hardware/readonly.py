"""Verify one 6404E through the project's SDK discovery, session and lock paths."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import re
from datetime import datetime
from pathlib import Path

from lab_control.application.environment import find_sdk
from lab_control.application.runtime import ApplicationRuntime
from lab_control.discovery.providers import VendorSdkDiscoveryProvider
from lab_control.domain.connections import ConnectionConfig
from lab_control.domain.instruments import DeviceError, jsonable, utc_now
from lab_control.instruments.common.readonly_hardware import asset_folder_name
from lab_control.instruments.devices.pico_6404e.adapter import PicoAdapter
from lab_control.instruments.devices.pico_6404e.hardware.records import publish_report
from lab_control.storage.hardware_documents import refresh_after_batch
from lab_control.storage.hardware_records import atomic_json
from lab_control.storage.repository import application_home


class ReadOnlyPico(PicoAdapter):
    """Run the production adapter with an audited allowlist for this acceptance session."""

    def __init__(self, config, transcript, library=None):
        self.transcript = transcript
        super().__init__(config, library)

    def call(self, name, types, *args, session=True, accepted=()):
        if name not in {"OpenUnit", "GetUnitInfo", "CloseUnit"}:
            raise DeviceError(f"Read-only verification refused ps6000a{name}", "readonly_violation")
        entry = {"at": utc_now(), "call": "ps6000a" + name}
        try:
            if name == "OpenUnit":
                entry.update(serial=args[1].decode("ascii"), resolution_enum=args[2])
                if entry["serial"] != self.config.expected_serial or args[2] != 0:
                    raise DeviceError("Expected exact serial and fixed 8-bit mode", "identity_conflict")
            elif name == "GetUnitInfo":
                entry["info"] = args[-1]
            super().call(name, types, *args, session=session, accepted=accepted)
            if name == "GetUnitInfo":
                entry["response"] = args[0].value.decode("ascii")
            entry["status"] = "PICO_OK"
        except Exception as exc:
            entry["error"] = str(exc)
            raise
        finally:
            with self.transcript.open("a", encoding="utf-8") as output:
                output.write(json.dumps(entry) + "\n")


def run(serial: str) -> tuple[Path, bool]:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9/-]*", serial):
        raise ValueError("Specify the instrument serial exactly as returned by PicoSDK")
    config = ConnectionConfig(
        "PICO::" + serial, "pico.6404e", "pico", expected_serial=serial, sdk_path=find_sdk("pico")
    )
    folder = (
        application_home()
        / "hardware-tests"
        / asset_folder_name("6404E", serial)
        / datetime.now().strftime("%Y-%m-%d-%H%M%S-%f-readonly")
    )
    folder.mkdir(parents=True, exist_ok=False)
    transcript = folder / "communication.jsonl"
    transcript.touch(exist_ok=False)
    stages = dict.fromkeys(
        (
            "enumeration",
            "identity",
            "connection",
            "reconnection",
            "disconnect",
            "parameter_read",
            "parameter_write",
            "restoration",
            "acquisition",
            "physical_output",
        ),
        "not_tested",
    )
    report = {
        "started_at": utc_now(),
        "adapter_id": config.adapter,
        "resource": config.resource,
        "config": jsonable(config),
        "expected_serial": serial,
        "stages": stages,
        "scope": "Enumerate, open the specified 6404E in its fixed 8-bit mode, read model/serial/firmware, and reconnect.",
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "sdk": config.sdk_path,
        },
        "source_sha256": {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (Path(__file__), Path(__file__).parents[1] / "adapter.py")
        },
        "setting_writes": 0,
    }
    stage = "enumeration"
    try:
        with ApplicationRuntime((config,)) as app:
            resources = [v.config.resource for v in VendorSdkDiscoveryProvider((config,)).enumerate()]
            with transcript.open("a", encoding="utf-8") as output:
                output.write(
                    json.dumps({"at": utc_now(), "call": "ps6000aEnumerateUnits", "resources": resources})
                    + "\n"
                )
            if config.resource not in resources:
                raise DeviceError("Expected serial missing from PicoSDK enumeration", "not_found")
            stages[stage] = "passed"
            device_id = app.add(config)

            def factory(selected):
                if (selected.resource, selected.adapter, selected.transport, selected.expected_serial) != (
                    config.resource,
                    config.adapter,
                    config.transport,
                    serial,
                ):
                    raise DeviceError("Unexpected asset configuration", "identity_conflict")
                return ReadOnlyPico(config, transcript)

            def admit(identity):
                if identity is not None and (identity.model, identity.serial) != ("6404E", serial):
                    raise DeviceError("Model or serial differs from the selected asset", "identity_conflict")

            app.sessions.factory = factory
            for stage in ("connection", "reconnection"):
                identity = app.connect(device_id, configure_acquisition=False, admission=admit).result(
                    timeout=90
                )
                report["identity"] = jsonable(identity)
                stages[stage] = stages["identity"] = "passed"
                stage = "disconnect"
                app.sessions.disconnect(device_id)
                stages[stage] = "passed"
        report["final_state"] = "Both sessions closed. No capture initiated."
    except (Exception, KeyboardInterrupt) as exc:
        code = getattr(exc, "code", type(exc).__name__)
        if code == "identity_conflict":
            stages["identity"] = "failed"
        stages[stage] = (
            "blocked"
            if code in {"process_busy", "resource_busy", "driver_missing", "not_found"}
            else "failed"
        )
        report.update(
            error=str(exc) or type(exc).__name__,
            error_code=code,
            final_state="See error and SDK transcript for the last confirmed state.",
        )
    passed = "error" not in report
    report.update(finished_at=utc_now(), checks_passed=passed)
    atomic_json(folder / "results.json", report)
    rows = "\n".join(f"| {key} | {value} |" for key, value in stages.items())
    (folder / "record.md").write_text(
        f"# PicoScope 6404E / {serial}\n\n| Stage | Result |\n|---|---|\n{rows}\n\n"
        "[Results](results.json) · [SDK transcript](communication.jsonl)\n",
        encoding="utf-8",
    )
    publish_report(folder)
    refresh_after_batch(folder)
    return folder, passed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", required=True)
    args = parser.parse_args()
    folder, passed = run(args.serial)
    print(folder)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
