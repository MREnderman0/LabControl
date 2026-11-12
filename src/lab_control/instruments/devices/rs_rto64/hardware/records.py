from pathlib import Path
from typing import Any

from lab_control.storage.hardware_records import matches, normalize

ADAPTER_ID = "rs.rto64"


def recognizes(report: dict[str, Any], folder: Path) -> bool:
    return matches(report, folder, ADAPTER_ID, ("RTO64",))


def summarize(report: dict[str, Any]) -> dict[str, Any]:
    from lab_control.domain.instruments import DeviceIdentity

    from ..adapter import canonical_model

    raw = report.get("identity")
    expected: tuple[str, ...] = ("RTO64",)
    if isinstance(raw, dict):
        identity = DeviceIdentity(*(str(raw.get(k) or "") for k in ("manufacturer", "model", "serial")))
        if canonical_model(identity) == "RTO64":
            expected = ("RTO64", identity.model)
    return normalize(report, adapter_id=ADAPTER_ID, model="RTO64", stage_names={}, expected_models=expected)


def publish_report(folder: Path) -> None:
    from lab_control.storage.hardware_records import publish_report as publish

    publish(folder, summarize)
