"""Offline import of DH-8318 asset records."""

from lab_control.storage.hardware_records import matches, normalize
from lab_control.storage.hardware_records import publish_report as publish

ADAPTER = "duoheyi.dh8318"
MODELS = ("DH-8318", "DH8318")


def recognizes(report, folder):
    return matches(report, folder, ADAPTER, MODELS)


def summarize(report):
    return normalize(report, adapter_id=ADAPTER, model="DH-8318", stage_names={}, expected_models=MODELS)


def publish_report(folder):
    publish(folder, summarize)
