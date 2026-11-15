"""Independent R&S FSPN-26 model definition."""

from pathlib import Path

from lab_control.domain.phase_noise_metrics import PHASE_NOISE_AT_OFFSET_PROFILE
from lab_control.domain.processing import ProcessingProfile
from lab_control.instruments.devices.definition import DeviceDefinition, DeviceDocumentation

from .adapter import Fspn26Adapter, profile
from .export import export_round


def _verify(*args, **kwargs):
    from .hardware.readonly import verify

    return verify(*args, **kwargs)


DEFINITION = DeviceDefinition(
    profile(),
    Fspn26Adapter,
    DeviceDocumentation(
        "R&S FSPN-26", "measurement-analyzers/phase-noise-fspn26", "LAN VISA", "64 位 VISA Runtime"
    ),
    Path(__file__).parent,
    deployment_components=("rs-visa",),
    supported_transports=("visa",),
    acquisition_kinds=("trace",),
    management_surface=True,
    monitoring_surface=False,
    acquisition_initiate_editable=False,
    metric_profiles=(PHASE_NOISE_AT_OFFSET_PROFILE,),
    offline_processing=ProcessingProfile("frames"),
    readonly_check=_verify,
    export_recorded_frame=export_round,
)
