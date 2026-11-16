from pathlib import Path
from typing import Any

from lab_control.domain.connections import ConnectionConfig
from lab_control.domain.instruments import AcquisitionRequest, Unsupported
from lab_control.instruments.base import ScpiAdapter
from lab_control.instruments.common import readonly_hardware
from lab_control.instruments.common.lecroy_ranges import MEMORY_QUERIES, PATHS, cvar_query

from .. import DEFINITION
from ..profile import CHANNELS
from .records import publish_report

QUERIES = (
    *(f"C{ch}:WF? ALL" for ch in CHANNELS),
    *MEMORY_QUERIES.values(),
    *(
        cvar_query(op, ch, member)
        for op in PATHS
        for ch in (CHANNELS if "{ch}" in PATHS[op] else (1,))
        for member in ("GetMinValue", "GetMaxValue", "GetGrainValue")
    ),
)


def capture_request(adapter: ScpiAdapter) -> AcquisitionRequest:
    channels = tuple(ch for ch in CHANNELS if adapter.read("channel_enabled", ch) == "ON")
    if not channels:
        raise Unsupported("No enabled analog channels on the panel")
    return AcquisitionRequest("waveform", channels=channels, initiate=False, allow_setup=False)


def verify(config: ConnectionConfig, folder: Path, **kwargs: Any) -> dict[str, Any]:
    return readonly_hardware.verify_asset(
        DEFINITION,
        config,
        folder,
        QUERIES,
        record_summary=publish_report,
        capture_request=capture_request,
        audit_transport=readonly_hardware.BinaryQueryOnlyTransport,
        **kwargs,
    )


if __name__ == "__main__":
    raise SystemExit(
        readonly_hardware.main(
            DEFINITION,
            QUERIES,
            publish_report,
            capture_request=capture_request,
            audit_transport=readonly_hardware.BinaryQueryOnlyTransport,
        )
    )
