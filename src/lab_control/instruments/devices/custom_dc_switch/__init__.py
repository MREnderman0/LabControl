"""Manually bound DC and channel switch board."""

from pathlib import Path

from lab_control.instruments.devices.definition import DeviceDefinition, DeviceDocumentation, DeviceModel

from .adapter import ACKS, ADAPTER_ID, MODEL, SOURCE, SwitchBoardAdapter, capability_schema
from .transport import create_transport


def verify_readonly(*args, **kwargs):
    from .hardware.readonly import verify

    return verify(*args, **kwargs)


DEFINITION = DeviceDefinition(
    None,
    None,
    DeviceDocumentation(
        MODEL,
        "temperature-load-drivers/dc-switch-board",
        "CP210x 串口，115200 8N1",
        "CP210x VCP 与 VISA",
    ),
    Path(__file__).parent,
    deployment_components=("rs-visa", "silabs-cp210x"),
    transport_factory=create_transport,
    transport_adapter=SwitchBoardAdapter,
    capability_schema=capability_schema,
    model=DeviceModel(ADAPTER_ID, ("自制",), (MODEL,), "output_switch", SOURCE, "现场确认"),
    supported_transports=("visa",),
    management_surface=True,
    monitoring_surface=False,
    manual_connection_only=True,
    manual_connection_label="手动绑定开关板",
    manual_connection_note="绑定时板端状态未知。点击即发送，首次发送会打开串口，可能使板卡复位。CH3 和 CH4 互斥。",
    direct_click_operations=tuple(ACKS),
    manual_usb_id=(0x10C4, 0xEA60),
    readonly_check=verify_readonly,
)
