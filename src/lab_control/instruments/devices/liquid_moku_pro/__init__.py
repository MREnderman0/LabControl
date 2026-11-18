"""Liquid Instruments Moku:Pro, existing MokuOS 4.4 four-slot workspace."""

from pathlib import Path

from lab_control.domain.processing import ProcessingProfile
from lab_control.instruments.devices.definition import DeviceDefinition, DeviceDocumentation, DeviceModel

from .adapter import ROLES, Adapter, schema
from .discovery import Discovery
from .protocol import ADAPTER, MODEL, Rest, serial_id


def probe(config):
    return Rest(config.resource, config.timeout).identity(config.expected_serial)


def role_page(controller, device_id, role):
    from .workspace import RolePage

    return RolePage(controller, device_id, role)


DEFINITION = DeviceDefinition(
    None,
    None,
    DeviceDocumentation(
        MODEL,
        "oscilloscopes/moku-pro",
        "LAN · Moku REST 4.4",
        "mDNS / REST；四槽配置使用官方 Moku 4.4 上位机",
    ),
    Path(__file__).parent,
    model=DeviceModel(
        ADAPTER,
        ("Liquid Instruments",),
        (MODEL,),
        "oscilloscope",
        "https://apis.liquidinstruments.com/api/getting-started/starting-mim.html",
        "4.4",
        "waveform/spectrum",
    ),
    factory=Adapter,
    capability_schema=schema,
    discovery_factory=Discovery,
    readonly_probe=probe,
    canonical_serial=serial_id,
    supported_transports=("moku",),
    role_page_factory=role_page,
    role_categories=tuple(ROLES),
    acquisition_kinds=("waveform", "spectrum"),
    acquisition_channel_selection="single",
    offline_processing=ProcessingProfile("frames"),
    deployment_components=("liquid-moku-desktop",),
)
