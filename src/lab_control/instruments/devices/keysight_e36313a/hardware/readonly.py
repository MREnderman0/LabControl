from lab_control.domain.instruments import AcquisitionRequest
from lab_control.instruments.common import readonly_hardware

from .. import DEFINITION
from .records import publish_report


def capture_request(adapter):
    return AcquisitionRequest("scalar", channels=adapter.profile.channels, initiate=False, allow_setup=False)


def verify(config, folder, **kwargs):
    return readonly_hardware.verify_asset(
        DEFINITION, config, folder, record_summary=publish_report, capture_request=capture_request, **kwargs
    )


if __name__ == "__main__":
    raise SystemExit(
        readonly_hardware.main(DEFINITION, record_summary=publish_report, capture_request=capture_request)
    )
