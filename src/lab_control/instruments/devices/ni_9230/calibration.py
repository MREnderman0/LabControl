"""Resolve CT12000L mounting records from the saved asset configuration."""

from dataclasses import dataclass
from math import isfinite

from lab_control.domain.instruments import ValidationError

from .daqmx import SENSITIVITY


@dataclass(frozen=True)
class Sensor:
    serial: str = ""
    sensitivity: float = SENSITIVITY
    source: str = "CT12000L nominal"

    def metadata(self):
        return {
            "sensor_serial": self.serial,
            "sensitivity_mV_per_g": self.sensitivity,
            "sensitivity_source": self.source,
        }


def sensors(options):
    result: dict[int, Sensor] = {}
    common = None
    for option in options:
        if not option.startswith("ct12000l."):
            continue
        try:
            name, value = option.removeprefix("ct12000l.").split("=")
            if name == "all":
                gain = float(value)
                if common is not None or not isfinite(gain) or gain <= 0:
                    raise ValidationError("Invalid or duplicate common CT12000L sensitivity")
                common = Sensor("", gain, "user_selected_common")
                continue
            serial, gain = value.split("@")
            ch = {"ai0": 1, "ai1": 2, "ai2": 3}[name]
            gain = float(gain)
        except (ValueError, KeyError) as exc:
            raise ValidationError("Expected ct12000l.aiN=serial@mV_per_g") from exc
        if (
            not serial.isascii()
            or not serial.isalnum()
            or not isfinite(gain)
            or gain <= 0
            or ch in result
            or any(s.serial == serial for s in result.values())
        ):
            raise ValidationError("Invalid or duplicate CT12000L sensor assignment")
        result[ch] = Sensor(serial, gain, "user_supplied")
    if result and common is not None:
        raise ValidationError("Choose either a common sensitivity or individual sensor assignments")
    return {ch: result.get(ch, common or Sensor()) for ch in (1, 2, 3)}
