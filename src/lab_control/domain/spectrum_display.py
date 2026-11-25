"""Physical units and bounds for a spectrum analyzer's amplitude graticule."""

import math

import numpy as np

# Each logarithmic unit is relative to the associated linear unit at value 1.
LOG_UNITS = {
    "dBm": "mW",
    "dBW": "W",
    "dBpW": "pW",
    "dBV": "V",
    "dBmV": "mV",
    "dBµV": "µV",
    "dBA": "A",
    "dBmA": "mA",
    "dBµA": "µA",
}
LINEAR_UNITS = {
    "W": ("power", 1),
    "mW": ("power", 1e-3),
    "pW": ("power", 1e-12),
    "V": ("voltage", 1),
    "mV": ("voltage", 1e-3),
    "µV": ("voltage", 1e-6),
    "A": ("current", 1),
    "mA": ("current", 1e-3),
    "µA": ("current", 1e-6),
}


def amplitude_unit(token: object) -> str | None:
    """Canonical unit of a raw reference-level setting, independent of the graticule."""
    name = str(token).strip().replace("µ", "u").replace("μ", "u").upper()
    for unit in (*LOG_UNITS, *LINEAR_UNITS):
        if unit.replace("µ", "u").upper() == name:
            return unit
    return {"VOLT": "V", "WATT": "W", "AMP": "A"}.get(name)


def convert_level(values, source, target, impedance=None):
    source, target = amplitude_unit(source), amplitude_unit(target)
    if source is None or target is None:
        raise ValueError("Unknown spectrum amplitude unit")
    if source == target:
        return values
    src, src_factor = LINEAR_UNITS[LOG_UNITS.get(source, source)]
    dst, dst_factor = LINEAR_UNITS[LOG_UNITS.get(target, target)]
    value = np.asarray(values)
    if source in LOG_UNITS and target in LOG_UNITS and src == dst:
        return value + (10 if src == "power" else 20) * math.log10(src_factor / dst_factor)
    if source in LOG_UNITS:
        value = np.power(10.0, value / (10 if src == "power" else 20))
    value = value * src_factor
    if src != dst:
        if impedance is None or impedance <= 0:
            raise ValueError("Conversion between voltage, current and power requires input impedance")
        if src == "voltage":
            value = value**2 / impedance
        elif src == "current":
            value = value**2 * impedance
        if dst == "voltage":
            value = np.sqrt(value * impedance)
        elif dst == "current":
            value = np.sqrt(value / impedance)
    value = value / dst_factor
    if target in LOG_UNITS:
        with np.errstate(divide="ignore", invalid="ignore"):
            value = (10 if dst == "power" else 20) * np.log10(value)
    return value


def panel_scale(
    unit, reference, *, span_db=None, reference_position=1.0, offset_db=0.0, percent=False, impedance=None
):
    """Reference position runs from bottom (0) to top (1). Offset is in dB."""
    unit = amplitude_unit(unit)
    if unit is None:
        raise ValueError("Unknown spectrum amplitude unit")
    linear = LOG_UNITS.get(unit, unit)
    family, _ = LINEAR_UNITS[linear]
    factor = 10 if family == "power" else 20
    if not math.isfinite(reference) or abs(reference) >= 9e37 or not 0 <= reference_position <= 1:
        raise ValueError("Invalid panel reference")
    if span_db is None:
        target = linear
        top = float(convert_level(reference, unit, target)) * 10 ** (offset_db / factor)
        lo, hi = 0.0, top
    else:
        if not 0 < span_db < 1e4:
            raise ValueError("Invalid panel span")
        target = unit if unit in LOG_UNITS else next(k for k, v in LOG_UNITS.items() if v == linear)
        top = float(convert_level(reference, unit, target)) + offset_db + (1 - reference_position) * span_db
        lo, hi = top - span_db, top
    if not all(math.isfinite(v) and abs(v) < 9e37 for v in (lo, hi)) or lo >= hi:
        raise ValueError("Invalid panel bounds")
    result = dict(lower=lo, upper=hi, unit=target)
    if impedance is not None:
        result["impedance"] = impedance
    if percent:
        result.update(unit="%", relative_unit=target, reference=hi, upper=100.0)
    return result


def display_values(values, unit, scale):
    if scale["unit"] == "%":
        return (
            100
            * convert_level(values, unit, scale["relative_unit"], scale.get("impedance"))
            / scale["reference"]
        )
    return convert_level(values, unit, scale["unit"], scale.get("impedance"))
