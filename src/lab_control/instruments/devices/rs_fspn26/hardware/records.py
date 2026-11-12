from lab_control.storage.hardware_records import matches, normalize
from lab_control.storage.hardware_records import publish_report as publish

from .. import DEFINITION


def recognizes(report, folder):
    return matches(report, folder, DEFINITION.adapter_id, ("FSPN-26",))


def summarize(report):
    return normalize(
        report,
        adapter_id=DEFINITION.adapter_id,
        model="FSPN-26",
        stage_names={},
        expected_models=("FSPN-26",),
    )


def publish_report(folder):
    publish(folder, summarize)
