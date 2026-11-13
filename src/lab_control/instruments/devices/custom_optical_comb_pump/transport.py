"""VISA ASRL setup for the pump's fixed binary frames."""

import re

from lab_control.domain.connections import ConnectionConfig
from lab_control.domain.instruments import ValidationError
from lab_control.transport.visa import RawSerialVisaTransport, VisaRuntime


def create_transport(config: ConnectionConfig, runtime: VisaRuntime) -> RawSerialVisaTransport:
    if config.transport != "visa" or not re.fullmatch(r"ASRL[1-9][0-9]*::INSTR", config.resource):
        raise ValidationError("请手动选择泵浦的 Windows COM 口")
    return RawSerialVisaTransport(runtime, config.resource, config.timeout, terminator="", baud_rate=115200)
