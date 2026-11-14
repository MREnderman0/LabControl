"""Keysight E36313A three-output DC supply."""

from pathlib import Path

from lab_control.domain.processing import ProcessingProfile
from lab_control.instruments.devices.definition import DeviceDefinition, DeviceDocumentation

from .adapter import E36313aAdapter, profile


def _readonly_check(*args, **kwargs):
    from .hardware.readonly import verify

    return verify(*args, **kwargs)


DEFINITION = DeviceDefinition(
    profile(),
    E36313aAdapter,
    DeviceDocumentation(
        "Keysight E36313A", "power-supplies/keysight-e36313a", "LAN / VXI-11", "64 位 VISA Runtime"
    ),
    Path(__file__).parent,
    offline_processing=ProcessingProfile("scalar"),
    deployment_components=("rs-visa",),
    supported_transports=("visa",),
    acquisition_kinds=("scalar",),
    acquisition_initiate_editable=False,
    readonly_check=_readonly_check,
)
