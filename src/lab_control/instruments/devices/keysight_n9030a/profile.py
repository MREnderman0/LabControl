"""Documented, passive subset of the PXA Swept SA measurement."""

from dataclasses import replace

from lab_control.domain.instruments import Parameter
from lab_control.instruments.base import Profile, read_command

from .protocol import point_count, scalar, token

ADAPTER_ID = "keysight.n9030a"
SOURCE = "https://www.keysight.com/me/en/assets/9018-06099/user-manuals/9018-06099.pdf"
TRACE_QUERY = "FETC:SAN1?"


def n9030a() -> Profile:
    commands = {}
    for name, query, unit, decode in (
        ("measurement_mode", "CONF?", "", token),
        ("start_frequency", "FREQ:STAR?", "Hz", scalar),
        ("stop_frequency", "FREQ:STOP?", "Hz", scalar),
        ("center_frequency", "FREQ:CENT?", "Hz", scalar),
        ("span", "FREQ:SPAN?", "Hz", scalar),
        ("sweep_time", "SWE:TIME?", "s", scalar),
        ("sweep_points", "SWE:POIN?", "", point_count),
        ("amplitude_unit", "UNIT:POW?", "", token),
        ("transfer_format", "FORM:DATA?", "", token),
        ("byte_order", "FORM:BORD?", "", token),
    ):
        spec = read_command(name, query, scoped=False)
        kind = "number" if decode is scalar else "integer" if decode is point_count else "text"
        cap = replace(spec.capability, parameters={"value": Parameter(kind, unit)})
        commands[name] = replace(spec, capability=cap, decode=decode)
    return Profile(
        ADAPTER_ID,
        ("Agilent Technologies", "Keysight Technologies"),
        ("N9030A",),
        "spectrum_analyzer",
        (1,),
        SOURCE,
        "PXA X-Series SA Mode User's & Programmer's Reference; Swept SA and numeric data format",
        commands,
        ("measurement_mode", "transfer_format"),
        acquisition="spectrum",
    )
