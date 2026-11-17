"""Keithley Integra 2701 with 7708 multiplexer cards."""

from pathlib import Path

from lab_control.domain.processing import ProcessingProfile
from lab_control.instruments.devices.definition import DeviceDefinition, DeviceDocumentation

from .adapter import Keithley2701Adapter, profile


def _verify(*args, **kwargs):
    from .hardware.readonly import verify

    return verify(*args, **kwargs)


DEFINITION = DeviceDefinition(
    profile(),
    Keithley2701Adapter,
    DeviceDocumentation(
        "Keithley 2701",
        "measurement-analyzers/keithley-2701",
        "以太网 / VISA SOCKET 1394",
        "64 位 VISA Runtime",
    ),
    Path(__file__).parent,
    socket_port=1394,
    deployment_components=("rs-visa",),
    monitoring_only=True,
    multi_channel_sources=("reading",),
    acquisition_requires_confirmation=True,
    acquisition_initiate_editable=False,
    offline_processing=ProcessingProfile("scalar"),
    readonly_check=_verify,
)
