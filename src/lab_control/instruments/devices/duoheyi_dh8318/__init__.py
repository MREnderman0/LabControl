"""DUOHEYI DH-8318 eight-channel electronic load."""

from pathlib import Path

from lab_control.domain.processing import ProcessingProfile
from lab_control.instruments.devices.definition import DeviceDefinition, DeviceDocumentation

from .adapter import Dh8318Adapter, create_transport, dh8318


def _readonly_check(*args, **kwargs):
    from .hardware.verify import verify_readonly

    return verify_readonly(*args, **kwargs)


DEFINITION = DeviceDefinition(
    dh8318(),
    Dh8318Adapter,
    DeviceDocumentation(
        "DUOHEYI DH-8318",
        "temperature-load-drivers/duoheyi-dh8318",
        "RS232 交叉线，按面板波特率，8N1，LF",
        "串口转接器驱动与 PySerial",
    ),
    Path(__file__).parent,
    deployment_components=("serial-interface",),
    supported_transports=("serial",),
    transport_factory=create_transport,
    identity_requires_adapter=True,
    monitoring_only=True,
    acquisition_kinds=("scalar",),
    acquisition_initiate_editable=False,
    multi_channel_sources=("voltage", "current"),
    offline_processing=ProcessingProfile("scalar"),
    readonly_check=_readonly_check,
)
