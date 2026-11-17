"""Verify a selected 2701 by acquiring and reopening a frame via the project session."""

import argparse
import csv
import hashlib
import json
import platform
from datetime import datetime
from pathlib import Path

import pyvisa

from lab_control.application.runtime import ApplicationRuntime
from lab_control.discovery.types import DiscoveryCandidate
from lab_control.domain.connections import ConnectionConfig
from lab_control.domain.instruments import AcquisitionRequest, DeviceError, jsonable, utc_now
from lab_control.instruments.common.readonly_hardware import asset_folder_name, record_trace
from lab_control.storage.hardware_documents import refresh_after_batch
from lab_control.storage.hardware_records import atomic_json
from lab_control.storage.repository import application_home

from ..adapter import Keithley2701Adapter
from .records import ADAPTER_ID, publish_report


class LoggedTransport:
    def __init__(self, transport, transcript):
        self.inner, self.transcript = transport, transcript
        self.resource = transport.resource

    def log(self, **fields):
        with self.transcript.open("a", encoding="utf-8") as stream:
            print(json.dumps(dict(time=utc_now(), **fields), ensure_ascii=False), file=stream)

    def open(self):
        self.inner.open()

    def read(self):
        raw = self.inner.read()
        self.log(reply=raw.decode("ascii", errors="backslashreplace"))
        return raw

    def close(self):
        self.inner.close()

    def query(self, command):
        self.log(query=command)
        raw = self.inner.query(command)
        self.log(reply=raw.decode("ascii", errors="backslashreplace"))
        return raw

    def write(self, command):
        self.log(write=command)
        self.inner.write(command)


def verify(config, channels=()):
    if config.adapter != ADAPTER_ID or config.transport != "visa" or not config.expected_serial.strip():
        raise ValueError("Supply the exact 2701 VISA resource and serial")
    folder = (
        application_home()
        / "hardware-tests"
        / asset_folder_name("Keithley-2701", config.expected_serial)
        / (datetime.now().strftime("%Y%m%d-%H%M%S-%f") + "-7708-capture")
    )
    folder.mkdir(parents=True, exist_ok=False)
    report = dict(
        adapter_id=ADAPTER_ID,
        started_at=utc_now(),
        resource=config.resource,
        expected_serial=config.expected_serial,
        scope="Configured 7708 inputs; identify, acquire, restore and reopen frame",
        environment=dict(python=platform.python_version(), pyvisa=pyvisa.__version__),
        stages={
            k: "not_tested"
            for k in (
                "identity",
                "connection",
                "acquisition",
                "recording",
                "recovery",
                "reconnect",
                "parameter_control",
                "physical_output",
            )
        },
        source_sha256={
            p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in Path(__file__).parents[1].glob("*.py")
        },
    )
    app = None
    stage = "identity"
    try:
        app = ApplicationRuntime((), hardware=True)
        owner = "keithley-2701-identity"
        app.leases.acquire(owner, (config.resource, "discovery:unknown_alias"))
        candidate = DiscoveryCandidate(config, "configured", safe_scpi=True)
        try:
            identity, adapter_id = app.probe(candidate)
            report["identity"] = jsonable(identity)
            if identity.serial != config.expected_serial or adapter_id != ADAPTER_ID:
                raise DeviceError("Unexpected identity at selected address", "identity_conflict")
        finally:
            app.leases.release(owner)
        report["stages"][stage] = "passed"
        stage = "connection"
        row = app.inventory.merge(candidate, folder.name, identity, ADAPTER_ID, reason="identified")
        device = row["id"]
        app.inventory.select_resource(device, config.resource, follow_address=False)
        factory = app.sessions.factory

        def logged(selected):
            adapter = factory(selected)
            assert isinstance(adapter, Keithley2701Adapter)
            adapter.transport = LoggedTransport(adapter.transport, folder / "communication.jsonl")
            return adapter

        app.sessions.factory = logged
        app.connect(device, configure_acquisition=False).result()
        session = app.sessions.sessions[device]

        def snapshot():
            adapter = session.adapter
            assert isinstance(adapter, Keithley2701Adapter)
            state = adapter._snapshot()
            state["functions"] = adapter._functions(
                adapter._channels(state["slot_serials"], state["scan_list"])
            )
            # On an autoranging input the instantaneous range can move. Compare its
            # policy and integration time, and the range only for fixed-range inputs.
            dcv = [ch for ch, function in state["functions"].items() if function == "VOLT:DC"]
            if dcv:
                inputs = ",".join(map(str, dcv))
                auto = adapter._query(f"VOLT:DC:RANG:AUTO? (@{inputs})").split(",")
                if len(auto) != len(dcv) or not set(auto) <= {"0", "1"}:
                    raise DeviceError("Invalid DCV autorange reply", "parse_error")
                state["dcv_autorange"] = auto
                state["dcv_nplc"] = adapter._query(f"VOLT:DC:NPLC? (@{inputs})")
                fixed = ",".join(str(ch) for ch, mode in zip(dcv, auto, strict=True) if mode == "0")
                if fixed:
                    state["dcv_fixed_ranges"] = adapter._query(f"VOLT:DC:RANG? (@{fixed})")
            return state

        before = session.worker.submit(snapshot).result()
        report.update(
            initial_snapshot=before, capabilities=jsonable(app.sessions.capability_snapshot(device))
        )
        report["stages"][stage] = "passed"
        stage = "acquisition"
        request = AcquisitionRequest(channels=tuple(channels) or tuple(before["functions"]), confirmed=True)
        try:
            frame = app.sessions.submit(device, "acquire", request).result()
            report["stages"][stage] = "passed"
        finally:
            after = session.worker.submit(snapshot).result()
            report["final_snapshot"] = after
            report["stages"]["recovery"] = "passed" if before == after else "failed"
            if before != after:
                raise DeviceError(
                    "Final configuration differs from the initial snapshot", "restoration_failed"
                )
        stage = "recording"
        report["traces"] = record_trace(frame, folder)
        with (folder / "channels.csv").open("w", newline="", encoding="utf-8-sig") as stream:
            writer = csv.writer(stream)
            writer.writerow(("slot", "channel", "function", "value", "unit", "received_at"))
            for m in frame.measurements:
                writer.writerow(
                    (
                        m.channel // 100,
                        m.channel,
                        m.metadata["function"],
                        m.values[0],
                        m.unit,
                        m.metadata["received_at"],
                    )
                )
        report["stages"][stage] = "passed"
        stage = "reconnect"
        app.sessions.disconnect(device)
        reconnected = app.connect(device, configure_acquisition=False).result()
        if reconnected != identity:
            raise DeviceError("Identity changed on reconnect", "identity_conflict")
        report["stages"][stage] = "passed"
    except Exception as exc:
        report["error"] = str(exc)
        report["stages"][stage] = {"state": "failed", "reason": str(exc)}
    finally:
        try:
            if app:
                app.close()
        finally:
            report["finished_at"] = utc_now()
            atomic_json(folder / "results.json", report)
            (folder / "record.md").write_text(
                "# Keithley 2701 / 7708\n\n"
                + "\n".join(f"- {k}: {v}" for k, v in report["stages"].items())
                + "\n\n逐路读数见 channels.csv，原始收发见 communication.jsonl，初末状态见 results.json。\n",
                encoding="utf-8",
            )
            publish_report(folder)
            refresh_after_batch(folder)
    return folder, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resource", required=True)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--channels", nargs="+", type=int, help="Subset of the instrument scan list")
    args = parser.parse_args()
    config = ConnectionConfig(args.resource, ADAPTER_ID, expected_serial=args.serial, timeout=5)
    folder, report = verify(config, args.channels or ())
    print(folder)
    print(report.get("error", report["stages"]))
    return int("error" in report)


if __name__ == "__main__":
    raise SystemExit(main())
