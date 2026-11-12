from lab_control.domain.instruments import AcquisitionRequest, DeviceError
from lab_control.instruments.common import readonly_hardware

from .. import DEFINITION
from ..protocol import flag
from .records import publish_report

QUERIES = (
    "FORM?",
    "EXP:WAV:MULT?",
    "EXP:WAV:INCX?",
    *(f"CHAN{ch}:DATA?" for ch in range(1, 5)),
    *(f"PROB{ch}:SET:ATT:UNIT?" for ch in range(1, 5)),
)


def capture_request(adapter):
    channels = tuple(
        ch for ch in adapter.profile.channels if flag(adapter.transport.query(f"CHAN{ch}:STAT?"))
    )
    if not channels:
        raise DeviceError("No enabled analog channel", "waveform_unavailable")
    return AcquisitionRequest("waveform", channels, allow_setup=False)


def verify(config, folder, **kwargs):
    return readonly_hardware.verify_asset(
        DEFINITION,
        config,
        folder,
        QUERIES,
        expected_model="RTO64",
        record_summary=publish_report,
        capture_request=capture_request,
        **kwargs,
    )


def main():
    return readonly_hardware.main(DEFINITION, QUERIES, publish_report, capture_request=capture_request)


if __name__ == "__main__":
    raise SystemExit(main())
