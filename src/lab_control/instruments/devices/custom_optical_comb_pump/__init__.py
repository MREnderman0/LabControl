"""Connection and protocol metadata for the custom RS232 pump."""

from pathlib import Path

from lab_control.domain.processing import ProcessingProfile
from lab_control.instruments.devices.definition import DeviceDefinition, DeviceDocumentation, DeviceModel

from .adapter import ADAPTER_ID, MODEL, SOURCE, OpticalCombPumpAdapter, capability_schema
from .transport import create_transport


def verify_readonly(*args, **kwargs):
    from .hardware.readonly import verify

    return verify(*args, **kwargs)


DEFINITION = DeviceDefinition(
    None,
    None,
    DeviceDocumentation(
        MODEL,
        "temperature-load-drivers/pump-driver",
        "RS232，自动验证 CH 1 协议，115200 8N1",
        "现有 VISA 运行库与实际串口适配器驱动",
    ),
    Path(__file__).parent,
    offline_processing=ProcessingProfile("scalar"),
    deployment_components=("rs-visa", "serial-interface"),
    transport_factory=create_transport,
    transport_adapter=OpticalCombPumpAdapter,
    capability_schema=capability_schema,
    model=DeviceModel(ADAPTER_ID, ("自制",), (MODEL,), "pump_driver", SOURCE, "未注明", "scalar"),
    supported_transports=("visa", "serial"),
    acquisition_kinds=("scalar",),
    acquisition_initiate_editable=False,
    identity_requires_adapter=True,
    serial_autoconnect="protocol",
    discovery_connection_only_transports=("visa", "serial"),
    manual_connection_label="手动连接泵浦",
    manual_connection_note="连接时核对 CH 1 六帧设置回复。可读取状态和关闭输出。参数写入与开启输出待核定。",
    readonly_check=verify_readonly,
)
