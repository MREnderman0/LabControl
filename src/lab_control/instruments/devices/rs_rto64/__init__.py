"""R&S RTO64: independent protocol and documentation package."""

from pathlib import Path

from lab_control.domain.processing import ProcessingProfile
from lab_control.domain.voltage_waveform_metrics import VOLTAGE_WAVEFORM_STATISTICS_PROFILE
from lab_control.instruments.devices.definition import DeviceDefinition, DeviceDocumentation

from .adapter import Rto64Adapter, rto64


def _verify(*args, **kwargs):
    from .hardware.readonly import verify

    return verify(*args, **kwargs)


DEFINITION = DeviceDefinition(
    rto64(),
    Rto64Adapter,
    DeviceDocumentation("R&S RTO64", "oscilloscopes/rto64", "LAN / USB VISA", "64 位 VISA Runtime"),
    Path(__file__).parent,
    offline_processing=ProcessingProfile("frames", separate_channel_pngs=True),
    readonly_check=_verify,
    deployment_components=("rs-visa",),
    multi_channel_sources=("waveform",),
    metric_profiles=(VOLTAGE_WAVEFORM_STATISTICS_PROFILE,),
)
