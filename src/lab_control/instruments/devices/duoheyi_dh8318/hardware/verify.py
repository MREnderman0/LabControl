"""Verify identity, passive V/A capture, saved data and reconnection on one DH-8318."""

from pathlib import Path
from typing import Any

from lab_control.domain.connections import ConnectionConfig
from lab_control.domain.instruments import AcquisitionRequest
from lab_control.instruments.base import ScpiAdapter
from lab_control.instruments.common import readonly_hardware

from .. import DEFINITION
from .records import publish_report

QUERIES = ("CH?", "MEAS:VOLT?", "MEAS:CURR?")


def capture_request(adapter: ScpiAdapter) -> AcquisitionRequest:
    return AcquisitionRequest(
        "scalar",
        channels=tuple(adapter.capability_details["acquisition_channels"]),
        points=1,
        allow_setup=False,
    )


def verify_readonly(config: ConnectionConfig, folder: Path, **kwargs: Any) -> dict[str, Any]:
    return readonly_hardware.verify_asset(
        DEFINITION,
        config,
        folder,
        QUERIES,
        record_summary=publish_report,
        capture_request=capture_request,
        **kwargs,
    )


if __name__ == "__main__":
    raise SystemExit(
        readonly_hardware.main(DEFINITION, QUERIES, publish_report, capture_request=capture_request)
    )
