"""VISA ASRL setup for the pump's fixed binary frames."""

import re

from lab_control.domain.connections import ConnectionConfig
from lab_control.domain.instruments import ValidationError
from lab_control.transport.visa import RawSerialVisaTransport, VisaRuntime


def create_transport(config: ConnectionConfig, runtime: VisaRuntime) -> RawSerialVisaTransport:
    port = re.fullmatch(r"COM([1-9][0-9]*)", config.resource, re.I)
    resource = f"ASRL{port[1]}::INSTR" if port else config.resource.upper()
    if config.transport not in {"visa", "serial"} or not re.fullmatch(r"ASRL[1-9][0-9]*::INSTR", resource):
        raise ValidationError("泵浦需要 Windows 串口资源")
    return RawSerialVisaTransport(runtime, resource, config.timeout, terminator="", baud_rate=115200)
