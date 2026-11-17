from pathlib import Path

from lab_control.storage.hardware_records import matches, normalize
from lab_control.storage.hardware_records import publish_report as publish_summary

ADAPTER_ID = "keithley.2701"


def recognizes(report, folder: Path):
    return matches(report, folder, ADAPTER_ID, ("Keithley-2701", "MODEL 2701"))


def summarize(report):
    return normalize(
        report, adapter_id=ADAPTER_ID, model="2701", stage_names={}, expected_models=("2701", "MODEL 2701")
    )


def publish_report(folder: Path):
    publish_summary(folder, summarize)
