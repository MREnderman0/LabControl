"""Serialized native single acquisitions. Model modules supply the trigger protocol."""

import math
import time
from dataclasses import replace
from threading import Event

from lab_control.domain.frames import validate_displayed_batch, validate_recorded_batch
from lab_control.domain.instruments import DeviceError, Quality
from lab_control.domain.waveform_coverage import mark_coverage


class SingleCapture:
    def __init__(self, request, *, restore=True, cancel=None):
        self.request = request
        self.restore = restore
        self.cancel = cancel if cancel is not None else Event()
        self.phase = "等待触发"

    def check(self):
        if self.cancel.is_set():
            raise DeviceError("已取消本次采集", "cancelled")

    def wait(self, complete, span, count):
        if not math.isfinite(span) or span <= 0 or not math.isfinite(count) or count < 1:
            raise DeviceError("示波器返回了无效采集跨度或次数", "invalid_parameter")
        deadline = time.monotonic() + span * count + 30
        while True:
            self.check()
            if complete():
                self.phase = "采集中"
                return
            if time.monotonic() >= deadline:
                raise DeviceError("等待本次触发完成超时", "capture_timeout")
            self.cancel.wait(0.05)

    def capture(self, adapter, read):
        self.check()
        self.request.validate()
        if self.request.kind != "waveform" or not self.request.confirmed:
            raise DeviceError("Single 需要已确认的波形请求", "confirmation_required")
        request = replace(self.request, consistency="full", context_id="")
        with adapter.single_acquisition(self, request) as (span, native_block):
            self.check()
            batch = read(replace(request, initiate=native_block))
            self.check()
            overloaded = [m.channel for m in batch.measurements if m.quality == Quality.OVERLOAD]
            if overloaded:
                channels = "、".join(f"CH {channel}" for channel in overloaded)
                raise DeviceError(f"{channels} 输入过载，请调大量程后重新采集", "waveform_overload")
            validate_displayed_batch(batch)
            validate_recorded_batch(batch)
            if sorted(m.channel for m in batch.measurements) != sorted(request.channels):
                raise DeviceError("单帧未返回全部所选通道", "incomplete_waveform")
            if native_block and any(len(m.values) != request.points for m in batch.measurements):
                raise DeviceError("单帧块采样点数不足", "incomplete_waveform")
            batch = mark_coverage(batch, span, require_full=True)
            return replace(batch, performance={**batch.performance, "single_completed": True})
