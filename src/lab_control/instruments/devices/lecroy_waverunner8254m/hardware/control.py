"""Verify controls and native Single on one registered VICP scope, then restore its panel."""

import argparse
import hashlib
import platform
import re
from datetime import datetime
from pathlib import Path

from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.instruments import AcquisitionRequest, DeviceCommand, DeviceError, jsonable, utc_now
from lab_control.instruments.common.lecroy_ranges import PATHS, cvar_query
from lab_control.instruments.common.readonly_hardware import BinaryQueryOnlyTransport, record_trace
from lab_control.instruments.scope_single import SingleCapture
from lab_control.storage.hardware_documents import refresh_after_batch
from lab_control.storage.hardware_records import atomic_json
from lab_control.storage.repository import application_home
from lab_control.transport.base import block_payload

from ..adapter import WaveRunner8254MAdapter
from ..profile import ADAPTER_ID, CHANNELS, MODEL
from .readonly import QUERIES
from .records import publish_report


class Audit(BinaryQueryOnlyTransport):
    enabled = False
    backup = b""

    def write(self, payload):
        if isinstance(payload, str) and payload in self.allowed:
            return super().write(payload)
        if not self.enabled:
            raise DeviceError("Panel backup required before writes", "restoration_failed")
        if isinstance(payload, bytes):
            if not self.backup or payload != self.backup:
                raise ValueError("Only the exact backed-up panel block can be recalled")
            self.record("tx", "PNSU <original panel block>")
        else:
            if "?" in payload or "\n" in payload or ";" in payload:
                raise DeviceError("Unexpected verification command", "unsupported")
            self.record("tx", payload)
        self.inner.write(payload)


def panel_block(raw):
    start = raw.find(b"#")
    if start < 0 or raw[:start].strip().upper() not in (b"", b"PNSU", b"PANEL_SETUP"):
        raise DeviceError("Invalid panel backup header", "parse_error")
    data = block_payload(raw[start:])
    if not data:
        raise DeviceError("Empty panel backup", "parse_error")
    return b"PNSU " + raw[start:]


def panel_settings_digest(raw):
    """Hash the complete setup, excluding only the MAUI export-time comment."""
    block = panel_block(raw).removeprefix(b"PNSU ")
    data = block_payload(block)
    header = data.split(b"\r\n", 3)
    if (
        len(header) == 4
        and header[0].startswith(b"' XStreamDSO ConfigurationVBScript")
        and re.fullmatch(rb"' [A-Za-z]+, [A-Za-z]+ \d{1,2}, \d{4} \d{1,2}:\d{2}:\d{2} [AP]M", header[2])
    ):
        data = b"\r\n".join((header[0], header[1], header[3]))
    return hashlib.sha256(data).hexdigest()


def verify(resource, serial, channel):
    if not serial.isalnum() or not resource.startswith("vicp://") or channel not in CHANNELS:
        raise ValueError("Specify the registered VICP resource, full serial and analog channel")
    folder = (
        application_home()
        / "hardware-tests"
        / f"{MODEL}-{serial}"
        / (datetime.now().strftime("%Y%m%d-%H%M%S-%f") + "-controls")
    )
    folder.mkdir(parents=True)
    report = {
        "scope": "Analog control write/readback, native Single and original panel restoration",
        "resource": resource,
        "expected_serial": serial,
        "adapter_id": ADAPTER_ID,
        "started_at": utc_now(),
        "environment": {"python": platform.python_version(), "host": platform.node()},
        "source_sha256": {
            str(p.relative_to(Path(__file__).parents[1])): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in [
                Path(__file__),
                Path(__file__).parents[1] / "adapter.py",
                Path(__file__).parents[1] / "control.py",
                Path(__file__).parents[1] / "profile.py",
            ]
        },
        "stages": dict.fromkeys(
            ("connection", "backup", "parameter_write", "single_capture", "restoration", "physical_output"),
            "not_tested",
        ),
        "operations": [],
    }
    stage = "connection"

    def save():
        atomic_json(folder / "results.json", report)

    try:
        with ApplicationRuntime() as app:
            row = app.inventory.by_resource(resource)
            if not row or row["adapterId"] != ADAPTER_ID or row["identity"]["serial"] != serial:
                raise DeviceError("Inventory identity mismatch", "identity_conflict")
            key = row["id"]
            previous_factory = app.sessions.factory

            def audited_factory(cfg):
                if cfg.resource != resource or cfg.adapter != ADAPTER_ID:
                    raise DeviceError("Resource mismatch", "identity_conflict")
                adapter = previous_factory(cfg)
                if not isinstance(adapter, WaveRunner8254MAdapter):
                    raise DeviceError("Adapter mismatch", "identity_conflict")
                queries = {"*IDN?", "*OPC?", "INR?", "PNSU?", "SEQ?", "ILVD?", "CHDR?", "BWL?", *QUERIES}
                queries.update(
                    cvar_query(op, ch, member)
                    for op in PATHS
                    for ch in CHANNELS
                    for member in ("GetRequestedValue", "GetAdaptedValue")
                )
                queries.update(
                    spec.query.format(ch=ch)
                    for spec in adapter.profile.commands.values()
                    for ch in CHANNELS
                    if spec.query
                )
                adapter.transport = Audit(adapter.transport, queries, folder / "communication.jsonl")
                return adapter

            app.sessions.factory = audited_factory
            ident = app.connect(key, configure_acquisition=False).result(90)
            if ident.serial != serial or ident.model.upper() != MODEL.upper():
                raise DeviceError("Live identity mismatch", "identity_conflict")
            report["identity"] = jsonable(ident)
            report["stages"][stage] = "passed"
            stage = "backup"
            session = app.sessions.sessions[key]
            adapter = session.adapter
            audit = adapter.transport

            def call(action):
                return session.worker.submit(action).result(120)

            def snapshot():
                fields = adapter.read_status().values.copy()
                for q in ("SEQ?", "ILVD?", "CHDR?", "BWL?"):
                    fields[q] = audit.query(q).decode("ascii").strip()
                return fields

            original = call(snapshot)
            report["initial_snapshot"] = original
            if (
                original["1.trigger_mode"] not in ("AUTO", "NORM", "STOP")
                or original[f"{channel}.channel_enabled"] != "ON"
            ):
                raise DeviceError(
                    "Select an enabled channel on a scope without a pending Single", "task_busy"
                )
            raw = call(lambda: audit.query_binary("PNSU?", 8 * 1024 * 1024))
            audit.backup = panel_block(raw)
            report["panel_backup_sha256"] = hashlib.sha256(raw).hexdigest()
            original_digest = panel_settings_digest(raw)
            report["initial_panel_settings_sha256"] = original_digest
            report["stages"][stage] = "passed"
            save()
            audit.enabled = True

            def apply(name, value=None, ch=1):
                cmd = DeviceCommand(name, {} if value is None else {"value": value}, ch, confirmed=True)
                entry = {"command": jsonable(cmd)}
                report["operations"].append(entry)
                save()
                result = app.sessions.submit(key, "apply", cmd).result(120)
                entry["result"] = jsonable(result)
                if result.outcome != "success":
                    raise DeviceError(f"{name}: {result.error}", "readback_mismatch")
                op = "trigger_mode" if name in {"run", "stop", "single"} else name
                actual = call(lambda: adapter.read(op, ch))
                entry["independent_readback"] = actual
                save()
                if actual != result.readback:
                    raise DeviceError(f"{name}: independent read differs", "readback_mismatch")

            def exercise(name, target, ch=1):
                old = original[f"{ch}.{name}"]
                if target == old:
                    raise ValueError(f"No-op test for {name}")
                apply(name, target, ch)
                apply(name, old, ch)

            try:
                stage = "parameter_write"
                ch = channel

                def old(name):
                    return original[f"{ch}.{name}"]

                level_range = adapter.capabilities_for_channel(ch).get("trigger_level").parameters["value"]
                step = old("scale") / 2
                level = old("trigger_level") + step
                if level > level_range.maximum:
                    level = old("trigger_level") - step
                for name, value in (
                    ("channel_enabled", "OFF"),
                    ("scale", old("scale") * 2),
                    ("offset", old("offset") + old("scale") / 2),
                    ("coupling", "A1M" if old("coupling") == "D1M" else "D1M"),
                    ("probe_attenuation", 1 if old("probe_attenuation") != 1 else 10),
                    ("trigger_level", level),
                    ("trigger_slope", "NEG" if old("trigger_slope") == "POS" else "POS"),
                    ("averages", 2 if old("averages") == 1 else 1),
                    ("bandwidth_limit", "20MHZ" if old("bandwidth_limit") != "20MHZ" else "FULL"),
                ):
                    exercise(name, value, ch)
                source = original["1.trigger_source"]
                exercise(
                    "trigger_source",
                    next(
                        f"C{c}"
                        for c in CHANNELS
                        if f"C{c}" != source and original[f"{c}.channel_enabled"] == "ON"
                    ),
                )
                exercise("timebase", original["1.timebase"] / 2)
                exercise("time_position", original["1.time_position"] + original["1.timebase"] / 2)
                # For an original hardware depth above the decoder budget, recall the saved panel below.
                depth = int(original["1.record_length"])
                apply("record_length", 100000 if depth != 100000 else 250000)
                if depth <= adapter.profile.commands["record_length"].capability.parameters["value"].maximum:
                    apply("record_length", depth)
                apply("stop")
                apply("run")
                apply("trigger_mode", "NORM")
                report["stages"][stage] = "passed"
                stage = "single_capture"
                request = AcquisitionRequest("waveform", (ch,), confirmed=True)
                # Set a real edge threshold inside the measured waveform so this test also works
                # on a quiet input. Capture completion must still come from the instrument.
                apply("trigger_mode", "AUTO")
                before = app.sessions.submit(key, "acquire", request).result(120)
                sample = before.measurements[0]
                low, high = float(min(sample.values)), float(max(sample.values))
                if not low < high:
                    raise DeviceError("Input has no measurable edge for Single", "waveform_unavailable")
                if call(lambda: adapter.read("trigger_source")) != f"C{ch}":
                    apply("trigger_source", f"C{ch}")
                apply("trigger_level", (low + high) / 2, ch)
                tokens = [sample.metadata["acquisition_key"]]
                for i in (1, 2):
                    restore = i == 2
                    if restore:
                        apply("run")
                    job = SingleCapture(request, restore=restore)
                    batch = app.sessions.submit_single(key, job).result(120)
                    token = batch.measurements[0].metadata["acquisition_key"]
                    if not batch.performance.get("single_completed") or not token or token in tokens:
                        raise DeviceError("No distinct completed Single trace", "incomplete_waveform")
                    mode = call(lambda: adapter.read("trigger_mode"))
                    if mode != ("AUTO" if restore else "STOP"):
                        raise DeviceError(f"Unexpected post-Single mode: {mode}", "readback_mismatch")
                    tokens.append(token)
                    output = folder / f"single-{i}"
                    output.mkdir()
                    report.setdefault("captures", []).append(
                        {"trigger": token, "mode_after": mode, "traces": record_trace(batch, output)}
                    )
                    save()
                report["stages"][stage] = "passed"
            finally:
                try:
                    actual = call(snapshot)

                    def read_panel_digest():
                        return panel_settings_digest(audit.query_binary("PNSU?", 8 * 1024 * 1024))

                    current_digest = call(read_panel_digest)
                    if actual != original or current_digest != original_digest:
                        # Recall the exact complete panel block; SCPI communication settings are
                        # outside that block, so verify those separately in the final snapshot.
                        def recall():
                            audit.write(audit.backup)
                            if audit.query("*OPC?").decode("ascii").strip().split()[-1] != "1":
                                raise DeviceError("Panel recall did not complete", "restoration_failed")

                        call(recall)
                        if call(lambda: adapter.read("trigger_mode")) != original["1.trigger_mode"]:
                            apply("trigger_mode", original["1.trigger_mode"])
                    actual = call(snapshot)
                    report["final_snapshot"] = actual
                    diff = {k: [v, actual.get(k)] for k, v in original.items() if actual.get(k) != v}
                    report["restoration_differences"] = diff
                    if diff:
                        raise DeviceError(f"Original panel differs: {diff}", "restoration_failed")
                    final_digest = call(read_panel_digest)
                    report["final_panel_settings_sha256"] = final_digest
                    if final_digest != original_digest:
                        raise DeviceError("Full panel differs after recall", "restoration_failed")
                    report["stages"]["restoration"] = "passed"
                except Exception as exc:
                    report["stages"]["restoration"] = "failed"
                    report["restoration_warning"] = str(exc)
                    raise
                finally:
                    audit.enabled = False
                    save()
    except BaseException as exc:
        if report["stages"][stage] != "passed":
            report["stages"][stage] = "blocked" if getattr(exc, "code", "") == "process_busy" else "failed"
        report["error"] = str(exc)
        raise
    finally:
        report["finished_at"] = utc_now()
        save()
        publish_report(folder)
        refresh_after_batch(folder)
        print(folder, report["stages"], flush=True)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resource", required=True)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--channel", type=int, choices=CHANNELS, required=True)
    args = parser.parse_args()
    verify(args.resource, args.serial, args.channel)
