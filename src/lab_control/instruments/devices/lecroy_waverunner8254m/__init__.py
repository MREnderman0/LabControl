"""Teledyne LeCroy WaveRunner 8254M model registration."""

from pathlib import Path

from lab_control.domain.processing import ProcessingProfile
from lab_control.instruments.devices.definition import DeviceDefinition, DeviceDocumentation

from .adapter import WaveRunner8254MAdapter
from .profile import profile


def _readonly_check(*args, **kwargs):
    from .hardware.readonly import verify

    return verify(*args, **kwargs)


DEFINITION = DeviceDefinition(
    profile(),
    WaveRunner8254MAdapter,
    DeviceDocumentation(
        "Teledyne LeCroy WaveRunner 8254M",
        "oscilloscopes/waverunner-8254m",
        "LAN / VICP（TCP/IP）或 VXI-11（LXI）",
        "VICP 使用标准套接字；VXI-11 使用 64 位 VISA Runtime",
    ),
    Path(__file__).parent,
    deployment_components=("rs-visa",),
    supported_transports=("vicp", "visa"),
    acquisition_kinds=("waveform",),
    acquisition_initiate_editable=False,
    offline_processing=ProcessingProfile("frames", separate_channel_pngs=True),
    multi_channel_sources=("waveform",),
    readonly_check=_readonly_check,
)
