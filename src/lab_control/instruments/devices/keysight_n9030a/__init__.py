"""Keysight/Agilent PXA N9030A spectrum analyzer."""

from pathlib import Path

from lab_control.domain.processing import ProcessingProfile
from lab_control.domain.spectrum_metrics import SPECTRUM_PEAK_AND_SNR_PROFILE
from lab_control.instruments.devices.definition import DeviceDefinition, DeviceDocumentation

from .adapter import N9030aAdapter
from .profile import n9030a


def _readonly_check(*args, **kwargs):
    from .hardware.readonly import verify

    return verify(*args, **kwargs)


DEFINITION = DeviceDefinition(
    n9030a(),
    N9030aAdapter,
    DeviceDocumentation(
        "Keysight / Agilent N9030A PXA",
        "spectrum-analyzers/keysight-n9030a",
        "LAN / VXI-11",
        "64 位 VISA Runtime",
    ),
    Path(__file__).parent,
    deployment_components=("rs-visa",),
    supported_transports=("visa",),
    acquisition_kinds=("spectrum",),
    acquisition_initiate_editable=False,
    offline_processing=ProcessingProfile("frames"),
    metric_profiles=(SPECTRUM_PEAK_AND_SNR_PROFILE,),
    readonly_check=_readonly_check,
)
