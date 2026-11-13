from lab_control.domain.instruments import AcquisitionRequest
from lab_control.instruments.common import readonly_hardware

from .. import DEFINITION
from ..adapter import TRACES, WINDOWS
from .records import publish_report

EXTRA_QUERIES = (
    "STAT:QUES:PNO:COND?",
    "STAT:QUES:FREQ:COND?",
    "INST:SEL?",
    "CONF:PNO:MEAS?",
    "LAY:CAT?",
    *(f"LAY:WIND{window}:TYPE?" for window in WINDOWS),
    *(
        f"DISP:WIND{window}:TRAC{trace}:{attribute}?"
        for window in WINDOWS
        for trace in TRACES
        for attribute in ("STAT", "RES")
    ),
    *(
        f"TRAC{window}{operation}? TRACE{trace}"
        for window in WINDOWS
        for trace in TRACES
        for operation in (":POIN", "")
    ),
)


def capture_request(adapter):
    return AcquisitionRequest("trace", allow_setup=False)


def verify(config, folder, **kwargs):
    return readonly_hardware.verify_asset(
        DEFINITION,
        config,
        folder,
        EXTRA_QUERIES,
        record_summary=publish_report,
        capture_request=capture_request,
        **kwargs,
    )


def main():
    return readonly_hardware.main(DEFINITION, EXTRA_QUERIES, publish_report, capture_request=capture_request)


if __name__ == "__main__":
    raise SystemExit(main())
