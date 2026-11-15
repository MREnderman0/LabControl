"""Synchronized native FSPN result reads. Each recorded run owns one sweep."""

import time

import numpy as np

from lab_control.domain.instruments import DeviceError, Measurement, MeasurementBatch, ValidationError
from lab_control.transport.base import numeric_values

from .adapter import LF_WINDOWS, MAX_POINTS, TRACES, catalog_fields, window_indices
from .settings import flag, snapshot

ADEV_WINDOWS = {"ADEV", "ALLAN DEVIATION"}


def validate_request(request):
    request.validate()
    active = request.kind == "phase_noise_round"
    if (
        request.kind not in {"phase_noise_round", "phase_noise_display"}
        or request.channels != (1,)
        or (request.initiate != active or (active and not (request.allow_setup and request.confirmed)))
    ):
        raise ValidationError("不支持此相噪测量请求")


def layout(adapter):
    raw = adapter.transport.query("LAY:CAT?")
    names = catalog_fields(raw)[::2]
    return [
        (index, name, adapter.query_token(f"LAY:WIND{index}:TYPE?"))
        for index, name in zip(window_indices(raw), names, strict=True)
    ]


def prepare_layout(adapter):
    windows = layout(adapter)
    for types, code in ((LF_WINDOWS, "PNO"), (ADEV_WINDOWS, "ADEV")):
        if any(kind in types for _, _, kind in windows):
            continue
        anchor = next((name for _, name, kind in windows if kind in LF_WINDOWS), windows[0][1])
        quoted = anchor.replace("'", "''")
        adapter.transport.query(f"LAY:ADD? '{quoted}',BEL,{code}")
        windows = layout(adapter)
        if not any(kind in types for _, _, kind in windows):
            raise DeviceError(f"仪器未建立 {code} 结果窗口", "acquisition_setup_blocked")


def positions(adapter, window, trace):
    p = f"CALC{window}:SNO"
    rows: list[tuple[float, float]] = []
    if flag(adapter, p + ":DEC"):
        x = array(adapter, f"{p}:TRAC{trace}:DEC:X?")
        y = array(adapter, f"{p}:TRAC{trace}:DEC:Y?")
        if len(x) != len(y):
            raise DeviceError("Spot Noise 频偏与电平数量不匹配", "parse_error")
        rows.extend(zip(x, y, strict=True))
    if flag(adapter, p + ":USER"):
        for index in range(1, 7):
            if flag(adapter, f"{p}{index}:STAT"):
                rows.append(
                    (
                        float(adapter.query_token(f"{p}{index}:X?")),
                        float(adapter.query_token(f"{p}{index}:TRAC{trace}:Y?")),
                    )
                )
    return [
        dict(window=window, trace=trace, offset_hz=float(x), dbc_hz=float(y))
        for x, y in rows
        if 0 < x < 1e30 and abs(y) < 1e30
    ]


def array(adapter, command):
    raw = adapter.transport.query(command)
    if not raw.strip():
        return np.empty(0)
    if raw.startswith(b"#"):
        raise DeviceError("仪器当前未返回 ASCII 格式的结果", "acquisition_setup_blocked")
    return numeric_values(raw)


def curve(adapter, window, trace, quantity, mode):
    count_query = f"TRAC{window}:POIN? TRACE{trace}"
    count = int(adapter.query_token(count_query))
    if count == 0:
        return None
    if not 0 < count <= MAX_POINTS:
        raise DeviceError("迹线点数超出单帧上限", "parse_error")
    data = array(adapter, f"TRAC{window}? TRACE{trace}")
    if data.size != 2 * count or int(adapter.query_token(count_query)) != count:
        raise DeviceError("迹线点数与回包不一致", "configuration_changed")
    x, y = data[::2], data[1::2]
    if (
        np.any(x <= 0)
        or np.any(np.diff(x) <= 0)
        or np.any(np.abs(data) >= 1e30)
        or (quantity == "adev" and np.any(y < 0))
    ):
        raise DeviceError("仪器返回无效的迹线坐标或测量值", "waveform_unavailable")
    pn = quantity == "phase_noise"
    return Measurement(
        quantity,
        "dBc/Hz" if pn else "1",
        y,
        1,
        x,
        "Hz" if pn else "s",
        metadata={"window": window, "trace": trace, "trace_mode": mode, "source": "instrument"},
    )


def read_result(adapter, active):
    windows = layout(adapter)
    settings = snapshot(adapter)
    if active and settings["carrier_missing"]:
        raise DeviceError("未找到待测载波，请检查 RF INPUT。", "signal_missing")
    if adapter.read("transfer_format") not in {"ASC", "ASCII", "ASC,0", "ASCII,0"}:
        raise DeviceError("当前迹线格式尚未验证，请使用 ASCII。", "acquisition_setup_blocked")
    curves, spots = [], []
    have_adev = False
    for window, _, kind in windows:
        if kind in ADEV_WINDOWS and not have_adev:
            item = curve(adapter, window, 1, "adev", "ADEV")
            if item is not None:
                curves.append(item)
                have_adev = True
        if kind not in LF_WINDOWS:
            continue
        for trace in TRACES:
            prefix = f"DISP:WIND{window}:TRAC{trace}"
            if not flag(adapter, prefix + ":STAT") or adapter.query_token(prefix + ":RES?") != "PN":
                continue
            mode = adapter.query_token(prefix + ":MODE?")
            if active and mode in {"VIEW", "BLAN", "BLANK"}:
                continue
            item = curve(adapter, window, trace, "phase_noise", mode)
            if item is None:
                if active:
                    raise DeviceError(f"窗口 {window} 迹线 {trace} 为 0 点", "waveform_unavailable")
                continue
            curves.append(item)
            spots.extend(positions(adapter, window, trace))
    if layout(adapter) != windows:
        raise DeviceError("读取期间结果窗口发生变化", "configuration_changed")
    have_pn = any(m.quantity == "phase_noise" for m in curves)
    if not curves or (active and not (have_pn and have_adev)):
        raise DeviceError(
            "仪器未返回有效的" + ("相噪谱" if not have_pn else " Allan 偏差") + "曲线", "waveform_unavailable"
        )
    cfg = dict(
        settings=settings,
        spot_noise=spots,
        origin="single_sweep" if active else "display",
        paired_curves=have_adev and have_pn,
    )
    return MeasurementBatch(adapter.identity, tuple(curves), cfg, validation="full")


def measure(adapter, request, stop):
    validate_request(request)
    adapter.check_pn()
    if stop.is_set():
        return
    if not request.initiate:
        result = read_result(adapter, False)
        if not stop.is_set():
            yield result
        return
    prepare_layout(adapter)
    estimate = snapshot(adapter)["estimated_s"]
    if stop.is_set():
        return
    adapter.transport.write("INIT:CONT OFF")
    adapter.query_token("*ESR?")
    pending = True
    began = time.monotonic()
    try:
        adapter.transport.write("INIT:IMM;*OPC")
        while not stop.is_set():
            status = int(adapter.query_token("*ESR?"))
            if status & 0x3C:
                raise DeviceError(
                    "仪器拒绝测量：" + adapter.query_token("SYST:ERR?"), "acquisition_setup_blocked"
                )
            if status & 1:
                pending = False
                result = read_result(adapter, True)
                if not stop.is_set():
                    yield result
                return
            elapsed = time.monotonic() - began
            if elapsed > max(120.0, 3 * (estimate or 60) + 60):
                if int(adapter.query_token("STAT:QUES:PNO:COND?")) & 2:
                    raise DeviceError("仪器搜索 RF INPUT 后未找到载波", "signal_missing")
                raise DeviceError("等待本轮相噪测量完成超时", "waveform_unavailable")
            stop.wait(0.25)
    finally:
        if pending:
            adapter.transport.write("ABOR")
            if adapter.query_token("*OPC?") not in {"1", "+1"}:
                raise DeviceError("仪器未确认停止", "cleanup_failed")
