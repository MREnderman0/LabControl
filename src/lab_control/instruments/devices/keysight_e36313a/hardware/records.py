from lab_control.storage.hardware_records import matches, normalize
from lab_control.storage.hardware_records import publish_report as publish

from .. import DEFINITION


def recognizes(report, folder):
    return matches(report, folder, DEFINITION.adapter_id, ("E36313A",))


def summarize(report):
    return normalize(
        report,
        adapter_id=DEFINITION.adapter_id,
        model="E36313A",
        stage_names={"scan": "enumeration", "connection": "compatibility_connection"},
        expected_models=("E36313A",),
    )


def publish_report(folder):
    publish(folder, summarize)
