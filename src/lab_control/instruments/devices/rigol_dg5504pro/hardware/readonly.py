"""DG5504 Pro identity, basic state and output read-only verification."""

from lab_control.instruments.common.readonly_hardware import main
from lab_control.instruments.devices.rigol_dg5504pro import DEFINITION

from .records import publish_report

EXTRA_QUERIES = tuple(
    query for ch in range(1, 5) for query in (f"OUTP{ch}?", f"SOUR{ch}:VOLT:HIGH?", f"SOUR{ch}:VOLT:LOW?")
)

if __name__ == "__main__":
    raise SystemExit(main(DEFINITION, EXTRA_QUERIES, record_summary=publish_report))
