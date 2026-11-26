"""Publish one acquired curve through OFC's existing writable capability contract."""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime
from itertools import count
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np

from lab_control.domain.frames import validate_recorded_batch
from lab_control.domain.instruments import Capability, DeviceError
from lab_control.storage.paths import filename_part

OPERATION = "获取单帧图像"
KINDS = {"oscilloscope": "waveform", "spectrum_analyzer": "spectrum"}
SHARE_ROOT = Path("E:/OFC-Share")
# Leave time for the v0.1.6 client's final get_task_status poll.
TIME_BUDGET = 27.0


def capability(definition):
    return Capability(
        OPERATION,
        writable=True,
        channel_scoped=True,
        source="LabControl",
        section="采集",
        requires_confirmation=definition.category == "oscilloscope"
        or definition.acquisition_initiate_default
        or definition.acquisition_requires_confirmation,
    )


def timeout_error():
    return DeviceError("单帧任务排队或图片保存超时", "capture_timeout")


class FrameImage:
    def __init__(self):
        self.deadline = time.monotonic() + TIME_BUDGET
        self.cancel = threading.Event()
        self.lock = threading.Lock()
        self.published = False

    def checkpoint(self):
        if time.monotonic() >= self.deadline:
            raise timeout_error()
        if self.cancel.is_set():
            raise DeviceError("单帧图像任务已停止", "cancelled")

    def expired(self):
        with self.lock:
            return not self.published and time.monotonic() >= self.deadline

    def save(self, batch, kind, channel, slot, definition, authorize):
        from lab_control.ui.widgets.publication_figure import export_figure

        self.checkpoint()
        validate_recorded_batch(batch)
        if len(batch.measurements) != 1 or batch.measurements[0].channel != channel:
            raise DeviceError(f"采集结果与所选通道 {channel} 不匹配", "acquisition_failed")
        m = batch.measurements[0]
        x, y = np.asarray(m.axis), np.asarray(m.values)
        if (
            m.axis is None
            or x.ndim != 1
            or x.shape != y.shape
            or not x.size
            or not m.axis_unit
            or not m.unit
            or not np.any(np.isfinite(x) & np.isfinite(y))
        ):
            raise DeviceError("采集帧缺少有效曲线、物理坐标或单位", "acquisition_failed")
        stamp = datetime.fromisoformat(batch.timestamp).astimezone()
        folder = SHARE_ROOT / "单帧图像" / stamp.date().isoformat()
        alias = getattr(definition.profile, "model_alias", None)
        model = alias(batch.identity) if alias else batch.identity.model
        spectrum = kind == "spectrum"
        trace = m.metadata.get("trace", m.channel)
        label = f"TRACE{trace}" if spectrum else f"CH{m.channel}"
        parts = [stamp.strftime("%H-%M-%S-") + f"{stamp.microsecond // 1000:03d}"]
        if slot:
            parts.append(slot)
        stem = "-".join(filename_part(p) for p in [*parts, model, label, "频谱" if spectrum else "波形"])
        axis = {"s": "Time", "Hz": "Frequency", "nm": "Wavelength"}.get(m.axis_unit, "X")
        ordinate = "Amplitude" if spectrum else "Voltage" if m.unit == "V" else "Amplitude"
        try:
            if not SHARE_ROOT.is_dir():
                raise FileNotFoundError(f"找不到共享目录 {SHARE_ROOT}")
            folder.mkdir(parents=True, exist_ok=True)
            with TemporaryDirectory(dir=folder, prefix=".frame-") as stage:
                png = Path(stage) / "frame.png"
                export_figure(
                    png,
                    [(label, x, y)],
                    f"{axis} ({m.axis_unit})",
                    f"{ordinate} ({m.unit})",
                    check_cancel=self.checkpoint,
                )
                authorize()
                for index in count(1):
                    suffix = "" if index == 1 else f"-{index:02d}"
                    target = folder / (stem + suffix + ".png")
                    with self.lock:
                        self.checkpoint()
                        try:
                            # Both operations publish an already closed file and refuse to overwrite.
                            if os.name == "nt":
                                png.rename(target)
                            else:
                                os.link(png, target)
                        except FileExistsError:
                            continue
                        self.published = True
                        break
        except OSError as exc:
            raise DeviceError(f"共享目录图片写入失败 {folder}：{exc}", "image_write_failed") from exc
        return {
            "operation": OPERATION,
            "outcome": "success",
            "path": str(target),
            "timestamp": batch.timestamp,
            "channel": m.channel,
            "series": label,
            "points": x.size,
        }
