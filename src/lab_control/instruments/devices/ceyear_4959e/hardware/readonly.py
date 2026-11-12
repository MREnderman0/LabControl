"""Read the configured analyzer's current SA trace through the shared verifier."""

from pathlib import Path
from typing import Any

from lab_control.domain.connections import ConnectionConfig
from lab_control.domain.instruments import AcquisitionRequest
from lab_control.instruments.base import ScpiAdapter
from lab_control.instruments.common import readonly_hardware

from .. import DEFINITION
from ..adapter import TRACE_QUERY
from .records import publish_report

EXTRA_QUERIES = (TRACE_QUERY,)


def capture_request(adapter: ScpiAdapter) -> AcquisitionRequest:
    return AcquisitionRequest("spectrum", channels=(1,), initiate=False, allow_setup=False)


def verify(config: ConnectionConfig, folder: Path, **kwargs: Any) -> dict[str, Any]:
    return readonly_hardware.verify_asset(
        DEFINITION,
        config,
        folder,
        EXTRA_QUERIES,
        record_summary=publish_report,
        capture_request=capture_request,
        **kwargs,
    )


if __name__ == "__main__":
    raise SystemExit(
        readonly_hardware.main(
            DEFINITION,
            EXTRA_QUERIES,
            publish_report,
            capture_request=capture_request,
        )
    )
