"""Read-only panel settings for WaveRunner 8254M."""

from dataclasses import replace
from functools import partial

from lab_control.instruments.base import Profile, read_command
from lab_control.instruments.common.lecroy_waveform import choice, numeric, transfer_setup, value_text

ADAPTER_ID = "lecroy.8254m"
MODEL = "WaveRunner8254M"
CHANNELS = (1, 2, 3, 4)
SOURCE = "https://cdn.teledynelecroy.com/files/manuals/maui-remote-control-and-automation-manual.pdf"


def profile() -> Profile:
    commands = {
        name: replace(read_command(name, query, scoped="{ch}" in query), decode=decode)
        for name, query, decode in (
            ("channel_enabled", "C{ch}:TRA?", partial(choice, choices=("ON", "OFF"))),
            ("scale", "C{ch}:VDIV?", numeric),
            ("offset", "C{ch}:OFST?", numeric),
            ("timebase", "TDIV?", numeric),
            ("time_position", "TRDL?", numeric),
            ("trigger_mode", "TRMD?", value_text),
            ("transfer_format", "CFMT?", value_text),
            ("byte_order", "CORD?", partial(choice, choices=("LO", "HI"))),
            ("waveform_setup", "WFSU?", transfer_setup),
        )
    }
    return Profile(
        ADAPTER_ID,
        ("LECROY",),
        (MODEL, "8254M"),
        "oscilloscope",
        CHANNELS,
        SOURCE,
        "MAUI Remote Control, waveform transfer and WAVEDESC",
        commands,
        tuple(commands),
        error_query="",
        acquisition="lecroy",
    )
