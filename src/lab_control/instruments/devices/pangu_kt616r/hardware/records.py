"""Offline summary for KT616R instrument batches."""

from pathlib import Path
from typing import Any

from lab_control.storage.hardware_records import matches, normalize

from ..adapter import ADAPTER_ID


def recognizes(report: dict[str, Any], folder: Path) -> bool:
    return matches(report, folder, ADAPTER_ID, ("KT616R",))


def summarize(report: dict[str, Any]) -> dict[str, Any]:
    return normalize(report, adapter_id=ADAPTER_ID, model="KT616R", stage_names={"reconnect": "reconnection"})
