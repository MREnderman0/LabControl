"""Teledyne LeCroy WaveRunner 9254: independent protocol and documentation package."""

from pathlib import Path

from lab_control.domain.processing import ProcessingProfile
from lab_control.domain.voltage_waveform_metrics import VOLTAGE_WAVEFORM_STATISTICS_PROFILE
from lab_control.instruments.devices.definition import DeviceDefinition, DeviceDocumentation

from .adapter import WaveRunner9254Adapter, lecroy

DEFINITION = DeviceDefinition(
    lecroy(),
    WaveRunner9254Adapter,
    DeviceDocumentation(
        "Teledyne LeCroy WaveRunner 9254",
        "oscilloscopes/waverunner-9254",
        "LAN VICP（1861）/ VISA VXI-11 或 USBTMC",
        "VICP 直接 TCP 不需要 VISA；其他路线需 VISA",
    ),
    Path(__file__).parent,
    offline_processing=ProcessingProfile("frames", separate_channel_pngs=True),
    deployment_components=("rs-visa",),
    multi_channel_sources=("waveform",),
    metric_profiles=(VOLTAGE_WAVEFORM_STATISTICS_PROFILE,),
)
