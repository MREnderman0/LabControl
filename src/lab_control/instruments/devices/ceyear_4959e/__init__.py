"""Ceyear 4959E microwave analyzer."""

from pathlib import Path

from lab_control.domain.processing import ProcessingProfile
from lab_control.domain.spectrum_metrics import SPECTRUM_PEAK_AND_SNR_PROFILE
from lab_control.instruments.devices.definition import DeviceDefinition, DeviceDocumentation

from .adapter import PROFILE, MicrowaveAnalyzer


def _verify(*args, **kwargs):
    from .hardware.readonly import verify

    return verify(*args, **kwargs)


DEFINITION = DeviceDefinition(
    PROFILE,
    MicrowaveAnalyzer,
    DeviceDocumentation(
        "思仪 4959E 微波综合测试仪", "spectrum-analyzers/ceyear-4959e", "LAN / VXI-11", "64 位 VISA Runtime"
    ),
    Path(__file__).parent,
    deployment_components=("rs-visa",),
    supported_transports=("visa",),
    acquisition_kinds=("spectrum",),
    acquisition_initiate_editable=False,
    offline_processing=ProcessingProfile("frames"),
    metric_profiles=(SPECTRUM_PEAK_AND_SNR_PROFILE,),
    readonly_check=_verify,
)
