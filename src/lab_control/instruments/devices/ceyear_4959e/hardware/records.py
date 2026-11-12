from lab_control.storage.hardware_records import matches, normalize
from lab_control.storage.hardware_records import publish_report as publish

ADAPTER = "ceyear.4959e"


def recognizes(report, folder):
    return matches(report, folder, ADAPTER, ("4959E",))


def summarize(report):
    return normalize(report, adapter_id=ADAPTER, model="4959E", stage_names={}, expected_models=("4959E",))


def publish_report(folder):
    publish(folder, summarize)
