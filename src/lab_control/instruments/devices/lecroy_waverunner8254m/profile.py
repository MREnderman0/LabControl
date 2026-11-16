"""WaveRunner 8254M analog panel controls and full-frame readback."""

from dataclasses import replace
from functools import partial

from lab_control.domain.instruments import Parameter
from lab_control.instruments.base import Profile, command, enum, parameter, read_command
from lab_control.instruments.common.lecroy_ranges import MEMORY_QUERIES, POINTS_MAX
from lab_control.instruments.common.lecroy_waveform import (
    choice,
    numeric,
    transfer_setup,
    trigger_source,
    value_text,
)

ADAPTER_ID = "lecroy.8254m"
MODEL = "WaveRunner8254M"
CHANNELS = (1, 2, 3, 4)
SOURCE = "https://cdn.teledynelecroy.com/files/manuals/maui-remote-control-and-automation-manual.pdf"


def source_readback(raw: bytes) -> str:
    # Some advanced trigger setups report only their type, with no analog SR qualifier.
    setup = value_text(raw).upper()
    if setup.startswith("EDGE,") or ",SR," in setup:
        return trigger_source(raw)
    return setup.partition(",")[0]


def profile() -> Profile:
    commands = {}
    for name, path, spec in (
        ("channel_enabled", "C{ch}:TRA", enum("ON", "OFF")),
        ("scale", "C{ch}:VDIV", parameter("V/div", 1e-6, 1e5)),
        ("offset", "C{ch}:OFST", parameter("V")),
        ("coupling", "C{ch}:CPL", enum("A1M", "D1M", "D50", "GND")),
        (
            "probe_attenuation",
            "C{ch}:ATTN",
            Parameter(
                "number",
                unit="ratio",
                choices=tuple(str(n) for n in (1, 2, 5, 10, 20, 25, 50, 100, 200, 500, 1000, 10000)),
            ),
        ),
        ("trigger_level", "C{ch}:TRLV", parameter("V")),
        ("trigger_slope", "C{ch}:TRSL", enum("POS", "NEG")),
        ("timebase", "TDIV", parameter("s/div", 1e-12, 1000)),
        ("time_position", "TRDL", parameter("s")),
        ("record_length", "MSIZ", Parameter("integer", minimum=1000, maximum=POINTS_MAX)),
        ("trigger_mode", "TRMD", enum("AUTO", "NORM", "SINGLE", "STOP")),
    ):
        commands[name] = replace(
            command(name, path, spec, scoped="{ch}" in path, confirm=name == "trigger_mode"),
            decode=value_text if spec.kind == "enum" else numeric,
        )
    commands["trigger_source"] = replace(
        command("trigger_source", "TRSE EDGE,SR,", enum("C1", "C2", "C3", "C4"), scoped=False, query="TRSE?"),
        writes=("TRSE EDGE,SR,{value}",),
        decode=source_readback,
    )
    for name, path, spec, decoder in (
        ("averages", "AverageSweeps", Parameter("integer", minimum=1, maximum=1024), numeric),
        ("bandwidth_limit", "BandwidthLimit", enum("FULL", "20MHZ", "200MHZ"), value_text),
    ):
        target = "app.Acquisition.C{ch}." + path
        write_value = '"{value}"' if spec.kind == "enum" else "{value}"
        commands[name] = replace(
            command(name, path, spec, query=f"VBS? 'return={target}'"),
            writes=(f"VBS '{target}={write_value}'",),
            decode=(lambda raw: value_text(raw).upper()) if spec.kind == "enum" else decoder,
        )
    for name, mode in (("run", "AUTO"), ("stop", "STOP"), ("single", "SINGLE")):
        commands[name] = replace(
            command(name, "TRMD " + mode, read=False, scoped=False, confirm=True, query="TRMD?"),
            decode=value_text,
        )
    for name, query, decode in (
        ("trigger_setup", "TRSE?", value_text),
        ("memory_mode", MEMORY_QUERIES["mode"], value_text),
        ("sample_rate", MEMORY_QUERIES["sample_rate"], numeric),
        ("transfer_format", "CFMT?", value_text),
        ("byte_order", "CORD?", partial(choice, choices=("LO", "HI"))),
        ("waveform_setup", "WFSU?", transfer_setup),
    ):
        commands[name] = replace(read_command(name, query, scoped=False), decode=decode)
    return Profile(
        ADAPTER_ID,
        ("LECROY",),
        (MODEL, "8254M"),
        "oscilloscope",
        CHANNELS,
        SOURCE,
        "MAUI Remote Control, waveform transfer and WAVEDESC",
        commands,
        tuple(name for name, spec in commands.items() if spec.capability.readable),
        error_query="",
        acquisition="lecroy",
    )
