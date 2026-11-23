"""Read DAQmx identity and physical channel capabilities without creating an acquisition task."""

from pathlib import Path

from lab_control.application.config import config_from_record
from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.instruments import DeviceError, jsonable, utc_now
from lab_control.storage.hardware_documents import refresh_after_batch
from lab_control.storage.hardware_records import atomic_json

from ..adapter import ID, Discovery
from .records import publish_report


def verify(config, folder, *, root=None, duration=1, input_condition=""):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "communication.jsonl").open("x", encoding="utf-8") as transcript:
        report = {
            "adapter_id": ID,
            "resource": config.resource,
            "expected_serial": config.expected_serial,
            "started_at": utc_now(),
            "scope": "NI-DAQmx module identity and terminal configurations",
            "config": jsonable(config),
            "environment": {"driver": "NI-DAQmx C API"},
            "stages": {"acquisition": "not_tested", "physical_output": "not_tested"},
        }
        stage = "enumeration"
        try:
            with ApplicationRuntime((), root=root) as app:
                if config.resource not in {c.config.resource for c in Discovery().enumerate()}:
                    raise DeviceError("Selected NI 9230 missing", "not_found")
                report["stages"][stage] = "passed"
                record = next(
                    (r for r in app.inventory.all() if r["config"]["resource"] == config.resource), None
                )
                if record and (
                    record["adapterId"] != ID
                    or (
                        config.expected_serial
                        and config_from_record(record).expected_serial not in ("", config.expected_serial)
                    )
                ):
                    raise DeviceError("Saved asset mismatch", "identity_conflict")
                device = record["id"] if record else app.add(config)
                for stage in ("connection", "reconnection"):
                    identity = app.connect(device, configure_acquisition=False).result(timeout=60)
                    report["identity"] = jsonable(identity)
                    report["stages"][stage] = report["stages"]["identity"] = "passed"
                    stage = "parameter_read"
                    for channel in (1, 2, 3):
                        readings = app.sessions.submit(
                            device, "read_settings", (("channel",), channel)
                        ).result(timeout=10)
                        import json

                        transcript.write(json.dumps(jsonable(readings)) + "\n")
                    report["stages"][stage] = "passed"
                    stage = "disconnect"
                    app.sessions.disconnect(device)
                    report["stages"][stage] = "passed"
            report["final_state"] = "Sessions closed; no DAQmx task created."
        except Exception as exc:
            report.update(error=str(exc), error_code=getattr(exc, "code", type(exc).__name__))
            report["stages"][stage] = (
                "blocked"
                if report["error_code"] in {"process_busy", "resource_busy", "not_found"}
                else "failed"
            )
        report.update(finished_at=utc_now(), checks_passed="error" not in report)
    atomic_json(folder / "results.json", report)
    (folder / "record.md").write_text(
        "# NI 9230 read-only check\n\n[Results](results.json) · [Channel queries](communication.jsonl)\n",
        encoding="utf-8",
    )
    publish_report(folder)
    refresh_after_batch(folder)
    return report
