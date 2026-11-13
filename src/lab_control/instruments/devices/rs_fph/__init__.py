"""Independent R&S Spectrum Rider FPH package."""

from pathlib import Path

from lab_control.domain.processing import ProcessingProfile
from lab_control.domain.spectrum_metrics import SPECTRUM_PEAK_AND_SNR_PROFILE
from lab_control.instruments.devices.definition import DeviceDefinition, DeviceDocumentation

from .adapter import FphAdapter
from .profile import fph


def _verify_trace(*args, **kwargs):
    from .hardware.trace_readonly import verify

    return verify(*args, **kwargs)


DEFINITION = DeviceDefinition(
    fph(),
    FphAdapter,
    DeviceDocumentation(
        "R&S FPH02 / FPH13", "spectrum-analyzers/rs-fph", "LAN；优先 PyVISA", "64 位 VISA Runtime"
    ),
    Path(__file__).parent,
    deployment_components=("rs-visa",),
    readonly_check=_verify_trace,
    offline_processing=ProcessingProfile("frames"),
    metric_profiles=(SPECTRUM_PEAK_AND_SNR_PROFILE,),
)
