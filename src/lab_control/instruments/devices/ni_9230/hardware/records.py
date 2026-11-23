from lab_control.storage.hardware_records import matches, normalize
from lab_control.storage.hardware_records import publish_report as publish

from ..adapter import ID
from ..daqmx import MODEL


def recognizes(report, folder):
    return matches(report, folder, ID, ("NI-9230-BNC",))


def summarize(report):
    summary = normalize(report, adapter_id=ID, model=MODEL, stage_names={}, expected_models=(MODEL,))
    if summary["resource"].startswith("NIDAQMX::"):
        summary.update(interface="USB", protocol="NI-DAQmx / CompactDAQ")
    return summary


def publish_report(folder):
    publish(folder, summarize)
