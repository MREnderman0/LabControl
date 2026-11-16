from pathlib import Path
from typing import Any

from lab_control.storage.hardware_records import matches, normalize
from lab_control.storage.hardware_records import publish_report as publish

from ..profile import ADAPTER_ID, MODEL


def recognizes(report: dict[str, Any], folder: Path) -> bool:
    return matches(report, folder, ADAPTER_ID, (MODEL.upper(), "8254M"))


def summarize(report: dict[str, Any]) -> dict[str, Any]:
    return normalize(
        report,
        adapter_id=ADAPTER_ID,
        model=MODEL,
        expected_models=(MODEL, "8254M"),
        stage_names={"scan": "enumeration", "connection": "compatibility_connection"},
    )


def publish_report(folder: Path) -> None:
    publish(folder, summarize)
