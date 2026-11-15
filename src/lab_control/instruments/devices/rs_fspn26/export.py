"""Publish the two native curves from one committed frame as a single directory."""

import csv
import os
from pathlib import Path
from tempfile import TemporaryDirectory


def export_round(folder: Path, frame: dict) -> None:
    curves = frame["measurements"]
    phase = [c for c in curves if c["quantity"] == "phase_noise"]
    allan = [c for c in curves if c["quantity"] == "adev"]
    if not phase or len(allan) != 1:
        raise ValueError("同轮相噪谱或 Allan 偏差不完整")
    target = folder / f"round-{frame['sequence']:04d}"
    names = ("phase_noise.csv", "allan_deviation.csv")
    if target.is_dir() and all((target / name).is_file() for name in names):
        return
    with TemporaryDirectory(dir=folder, prefix=".curve-export-") as stage:
        ready = Path(stage) / "curves"
        ready.mkdir()
        for filename, group in zip(names, (phase, allan), strict=True):
            with (ready / filename).open("w", newline="", encoding="utf-8") as out:
                writer = csv.writer(out)
                is_phase = group is phase
                writer.writerow(
                    ("window", "trace", "offset_hz", "phase_noise_dbc_hz") if is_phase else ("tau_s", "adev")
                )
                for c in group:
                    meta = c["metadata"]
                    prefix = (meta["window"], meta["trace"]) if is_phase else ()
                    writer.writerows((*prefix, x, y) for x, y in zip(c["axis"], c["values"], strict=True))
                out.flush()
                os.fsync(out.fileno())
        ready.rename(target)
