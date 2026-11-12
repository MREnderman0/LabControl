"""Pangu KT616R Ethernet recorder."""

from pathlib import Path

from lab_control.domain.processing import ProcessingProfile
from lab_control.instruments.devices.definition import DeviceDefinition, DeviceDocumentation, DeviceModel

from .adapter import ADAPTER_ID, RecorderAdapter, capability_schema
from .protocol import SOURCE, create_transport, discovery_config


def _verify(*args, **kwargs):
    from .hardware.readonly import verify

    return verify(*args, **kwargs)


DEFINITION = DeviceDefinition(
    None,
    None,
    DeviceDocumentation(
        "盘古 KT616R",
        "measurement-analyzers/pangu-kt616r",
        "以太网 / Modbus TCP",
        "Python 标准库 TCP，无专用驱动",
    ),
    Path(__file__).parent,
    deployment_components=("msvc-runtime",),
    socket_port=502,
    discovery_config=discovery_config,
    model=DeviceModel(
        ADAPTER_ID,
        ("Pangu", "盘古"),
        ("KT616R",),
        "multimeter",
        SOURCE,
        "KT600 User’s Manual §13.5–13.6",
        "scalar",
    ),
    transport_factory=create_transport,
    transport_adapter=RecorderAdapter,
    capability_schema=capability_schema,
    identity_requires_adapter=True,
    supported_transports=("tcp",),
    monitoring_only=True,
    acquisition_kinds=("scalar",),
    acquisition_initiate_editable=False,
    offline_processing=ProcessingProfile("scalar"),
    readonly_check=_verify,
)
