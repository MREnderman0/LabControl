"""Verify continuous scalar monitoring and recording with the production runtime."""

import argparse
import hashlib
import json
import platform
import re
import time
from datetime import datetime
from pathlib import Path

import numpy as np

from lab_control.application.controls import data_sources
from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.instruments import DeviceError, jsonable, utc_now
from lab_control.instruments.common.readonly_hardware import asset_folder_name
from lab_control.storage.hardware_documents import refresh_after_batch
from lab_control.storage.hardware_records import atomic_json
from lab_control.storage.recording import read_frames
from lab_control.storage.repository import application_home

from ..adapter import ID, Discovery
from ..daqmx import MODEL
from .records import publish_report


def run(serial, channels):
    if (
        not re.fullmatch("[A-F0-9]{8}", serial)
        or not channels
        or not set(channels) <= {1, 2, 3}
        or len(set(channels)) != len(channels)
    ):
        raise ValueError("Expected eight-digit serial and distinct CH1–CH3 inputs")
    folder = (
        application_home()
        / "hardware-tests"
        / asset_folder_name("NI-9230-BNC", serial)
        / datetime.now().strftime("%Y-%m-%d-%H%M%S-%f-scalar-monitor")
    )
    folder.mkdir(parents=True)
    stages = dict.fromkeys(
        (
            "enumeration",
            "identity",
            "connection",
            "parameter_read",
            "parameter_write",
            "reconnection",
            "acquisition",
            "export_readback",
            "disconnect",
            "restoration",
            "physical_output",
        ),
        "not_tested",
    )
    report = dict(
        adapter_id=ID,
        resource="NIDAQMX::" + serial,
        expected_serial=serial,
        started_at=utc_now(),
        stages=stages,
        requested_channels=channels,
        scope="CT12000L acceleration using saved sensor assignments or nominal sensitivity; 4 mA IEPE, AC, 3200 S/s, one point per interval",
        environment={"platform": platform.platform(), "python": platform.python_version()},
        source_sha256={
            f.name: hashlib.sha256(f.read_bytes()).hexdigest()
            for f in (
                Path(__file__),
                Path(__file__).parents[1] / "adapter.py",
                Path(__file__).parents[1] / "daqmx.py",
            )
        },
    )
    stage = "enumeration"
    try:
        with ApplicationRuntime(()) as app:
            config = next(
                (c.config for c in Discovery().enumerate() if c.config.resource == report["resource"]), None
            )
            if config is None:
                raise DeviceError("Selected module missing", "not_found")
            stages[stage] = "passed"
            record = next(
                (r for r in app.inventory.all() if r["config"]["resource"] == config.resource), None
            )
            if record and record["adapterId"] != ID:
                raise DeviceError("Saved asset adapter differs", "identity_conflict")
            device = record["id"] if record else app.add(config)
            report["device_id"] = device
            for stage in ("connection", "reconnection"):
                identity = app.connect(device, configure_acquisition=False).result(timeout=65)
                if (identity.model, identity.serial) != (MODEL, serial):
                    raise DeviceError("Asset identity mismatch", "identity_conflict")
                stages[stage] = stages["identity"] = "passed"
                report["identity"] = jsonable(identity)
                if stage == "connection":
                    report["initial_status"] = jsonable(
                        app.sessions.submit(device, "status").result(timeout=10)
                    )
                    app.sessions.disconnect(device)
            try:
                stage = "parameter_read"
                report["input_settings"] = {
                    f"ai{ch - 1}": jsonable(
                        app.sessions.submit(device, "read_settings", (("channel",), ch)).result(10)
                    )
                    for ch in channels
                }
                stages[stage] = "passed"
                stage = "acquisition"
                request = data_sources(config)[0].request(channels, 1, True, True)
                app.monitoring.start(device, (request,), interval=0.5, record_interval=None, recording=True)
                task = app.monitoring.tasks[device]
                deadline = time.monotonic() + 65
                while time.monotonic() < deadline:
                    snap = app.monitoring.snapshots()[device]
                    if snap["error"]:
                        raise DeviceError(snap["error"], "monitoring_failed")
                    if task.recorder and task.recorder.snapshot()["received"] >= 5:
                        break
                    time.sleep(0.1)
                else:
                    raise DeviceError("未取得五个有效监测帧：" + snap.get("notice", ""), "read_timeout")
                app.monitoring.stop(device).result(timeout=30)
                stages[stage] = "passed"
                stage = "export_readback"
                paths = task.recorder.finish().result(timeout=30)
                frames = [frame for path in paths for frame in read_frames(Path(path))]
                if len(frames) != task.recorder.snapshot()["committed"] or len(frames) < 5:
                    raise ValueError("Saved frame count differs from recorder readback")
                previous_scan = -1
                for frame in frames:
                    states = frame["configuration"]["channel_status"]
                    good = {ch for ch in channels if states[f"ai{ch - 1}"] == "正常"}
                    samples = frame["measurements"]
                    if {m["channel"] for m in samples} != good or len(samples) != len(good):
                        raise ValueError("Valid input list differs from archived frame")
                    scans = {m["metadata"]["scan_index"] for m in samples}
                    if len(scans) != 1 or min(scans) <= previous_scan:
                        raise ValueError("Expected one fresh synchronous hardware scan per tick")
                    previous_scan = min(scans)
                    for sample in samples:
                        if (
                            sample["quantity"] != "acceleration"
                            or sample["unit"] != "g"
                            or np.shape(sample["values"]) != (1,)
                            or not np.isfinite(sample["values"][0])
                        ):
                            raise ValueError("Expected one finite acceleration value per healthy input")
                report.update(
                    project_records=paths, frames=len(frames), configuration=frames[-1]["configuration"]
                )
                stages[stage] = "passed"
            finally:
                app.monitoring.stop(device).result(timeout=30)
                app.sessions.disconnect(device)
                stages["disconnect"] = "passed"
                report["final_state"] = (
                    "DAQmx task cleared and session disconnected; physical current not independently measured."
                )
    except Exception as exc:
        report.update(error=str(exc), error_code=getattr(exc, "code", type(exc).__name__))
        stages[stage] = (
            "blocked"
            if report["error_code"] in {"resource_busy", "process_busy", "driver_missing", "not_found"}
            else "failed"
        )
    report["finished_at"] = utc_now()
    atomic_json(folder / "results.json", report)
    (folder / "record.md").write_text(
        "# NI 9230 (BNC) / "
        + serial
        + "\n\n"
        + "\n".join(f"- {k}: {v}" for k, v in stages.items())
        + "\n\n[Results](results.json)\n",
        encoding="utf-8",
    )
    publish_report(folder)
    refresh_after_batch(folder)
    print(folder)
    print(json.dumps(report, ensure_ascii=True))
    return int("error" in report)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--channels", type=int, nargs="+", required=True)
    args = parser.parse_args()
    return run(args.serial.upper(), tuple(args.channels))


if __name__ == "__main__":
    raise SystemExit(main())
