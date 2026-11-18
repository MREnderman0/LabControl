from pathlib import Path

from lab_control.storage.hardware_records import matches, normalize
from lab_control.storage.hardware_records import publish_report as publish

from ..protocol import ADAPTER, MODEL


def recognizes(report, folder: Path):
    return matches(report, folder, ADAPTER, (MODEL,))


def summarize(report):
    summary = normalize(report, adapter_id=ADAPTER, model=MODEL, stage_names={}, expected_models=(MODEL,))
    summary.update(
        interface="LAN",
        protocol="Moku REST" if report.get("resource", "").startswith("moku://") else "mDNS/DNS-SD",
    )
    return summary


def publish_report(folder):
    publish(folder, summarize)
