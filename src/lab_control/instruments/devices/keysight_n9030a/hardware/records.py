from pathlib import Path
from typing import Any

from lab_control.storage.hardware_records import matches, normalize
from lab_control.storage.hardware_records import publish_report as publish

from ..profile import ADAPTER_ID


def recognizes(report: dict[str, Any], folder: Path) -> bool:
    return matches(report, folder, ADAPTER_ID, ("N9030A",))


def summarize(report: dict[str, Any]) -> dict[str, Any]:
    return normalize(
        report,
        adapter_id=ADAPTER_ID,
        model="N9030A",
        stage_names={"scan": "enumeration", "connection": "compatibility_connection"},
        expected_models=("N9030A",),
    )


def publish_report(folder: Path) -> None:
    publish(folder, summarize)
