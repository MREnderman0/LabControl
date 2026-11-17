"""Verify identity and static setup without switching any relay."""

from lab_control.instruments.common import readonly_hardware

from .. import DEFINITION
from .records import publish_report

SLOT_QUERIES = ("SYST:CARD1:SNUM?", "SYST:CARD2:SNUM?")


def verify(config, folder, **kwargs):
    return readonly_hardware.verify_asset(
        DEFINITION, config, folder, SLOT_QUERIES, record_summary=publish_report, **kwargs
    )


if __name__ == "__main__":
    raise SystemExit(readonly_hardware.main(DEFINITION, SLOT_QUERIES, record_summary=publish_report))
