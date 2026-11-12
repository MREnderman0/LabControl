"""Verify the selected N9030A via the shared query-only hardware session."""

from pathlib import Path

from lab_control.domain.connections import ConnectionConfig
from lab_control.domain.instruments import AcquisitionRequest
from lab_control.instruments.base import ScpiAdapter
from lab_control.instruments.common import readonly_hardware

from .. import DEFINITION
from ..profile import TRACE_QUERY
from .records import publish_report


def capture_request(adapter: ScpiAdapter) -> AcquisitionRequest:
    return AcquisitionRequest(kind="spectrum", channels=(1,), initiate=False, allow_setup=False)


def verify(config: ConnectionConfig, folder: Path, **kwargs) -> dict:
    return readonly_hardware.verify_asset(
        DEFINITION,
        config,
        folder,
        (TRACE_QUERY,),
        record_summary=publish_report,
        capture_request=capture_request,
        **kwargs,
    )


if __name__ == "__main__":
    raise SystemExit(
        readonly_hardware.main(
            DEFINITION,
            (TRACE_QUERY,),
            record_summary=publish_report,
            capture_request=capture_request,
        )
    )
