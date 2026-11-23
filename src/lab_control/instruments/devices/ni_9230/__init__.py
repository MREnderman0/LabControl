"""National Instruments NI 9230 three-channel IEPE input."""

from pathlib import Path

from lab_control.domain.processing import ProcessingProfile
from lab_control.instruments.devices.definition import DeviceDefinition, DeviceDocumentation, DeviceModel

from .adapter import CATEGORY, ID, Adapter, Discovery, capability_schema
from .daqmx import MODEL


def readonly_check(*args, **kwargs):
    from .hardware.readonly import verify

    return verify(*args, **kwargs)


DEFINITION = DeviceDefinition(
    None,
    None,
    DeviceDocumentation(
        "NI 9230 (BNC)",
        "measurement-analyzers/ni-9230",
        "USB cDAQ / NI-DAQmx",
        "NI-DAQmx 26.8 x64 / nicaiu.dll",
    ),
    Path(__file__).parent,
    model=DeviceModel(
        ID,
        ("National Instruments",),
        (MODEL,),
        CATEGORY,
        "NI 9230 Specifications",
        "NI-DAQmx C API 26.8",
        "scalar",
    ),
    factory=Adapter,
    capability_schema=capability_schema,
    discovery_factory=Discovery,
    readonly_check=readonly_check,
    supported_transports=("nidaqmx",),
    identity_requires_adapter=True,
    monitoring_only=True,
    acquisition_kinds=("scalar",),
    acquisition_initiate_default=True,
    acquisition_initiate_editable=False,
    acquisition_requires_confirmation=True,
    multi_channel_sources=("acceleration",),
    deployment_components=("ni-daqmx",),
    offline_processing=ProcessingProfile(
        "scalar", separate_channel_pngs=True, volatile_configuration_fields=("channel_status",)
    ),
)
