"""Passive readback of a displayed single-sideband phase-noise trace."""

import csv
from dataclasses import replace

import numpy as np

from lab_control.domain.instruments import Capability, DeviceError, Measurement, Unsupported
from lab_control.instruments.base import CommandSpec, Profile, ScpiAdapter, read_command
from lab_control.transport.base import numeric_values

from .settings import SETTINGS, apply_settings, snapshot

SOURCE = "https://www.rohde-schwarz.com/us/manual/rs-fspn-user-manual_78701-1129408.html"
MAX_POINTS = 4_000_000
WINDOWS = range(1, 17)
TRACES = range(1, 7)
# Window types with the single-sideband L(f) scale, excluding Sphi, Sv and Sy.
LF_WINDOWS = {"PNO", "NOISE SPECTRUM", "NOISE SPECTRUM L(F)", "PN NOISE SPECTRUM"}


def token(raw: bytes) -> str:
    return raw.decode("ascii").strip().strip("'\"").upper()


def profile() -> Profile:
    commands = {
        "applications": replace(
            read_command("applications", "INST:LIST?", scoped=False), decode=catalog_fields
        ),
        "transfer_format": replace(read_command("transfer_format", "FORM?", scoped=False), decode=token),
    }
    commands["phase_noise_settings"] = SETTINGS
    commands["phase_noise_round"] = CommandSpec(Capability("phase_noise_round", section="11.5.3"))
    commands["phase_noise_trace"] = CommandSpec(Capability("phase_noise_trace", section="11.5.3 / 11.7.3"))
    return Profile(
        "rs.fspn26",
        ("Rohde&Schwarz", "Rohde & Schwarz"),
        ("FSPN-26",),
        "phase_noise_analyzer",
        (1,),
        SOURCE,
        "1179.4363.02 v06",
        commands,
        ("applications", "transfer_format"),
        acquisition="trace",
    )


def catalog_fields(raw: bytes) -> list[str]:
    text = raw.decode("ascii").strip()
    fields = next(csv.reader([text], quotechar="'" if text.startswith("'") else '"', skipinitialspace=True))
    if not fields or len(fields) % 2 or not all(fields):
        raise DeviceError("Invalid FSPN catalog", "parse_error")
    return fields


def window_indices(raw: bytes) -> tuple[int, ...]:
    fields = catalog_fields(raw)
    try:
        windows = tuple(int(field.strip("'\" ")) for field in fields[1::2])
    except ValueError as exc:
        raise DeviceError("Invalid FSPN window index", "parse_error") from exc
    if len(set(windows)) != len(windows) or any(w not in WINDOWS for w in windows):
        raise DeviceError("Invalid FSPN window catalog", "parse_error")
    return windows


class Fspn26Adapter(ScpiAdapter):
    def query_token(self, command):
        return token(self.transport.query(command))

    recorded_run_kinds = ("phase_noise_round", "phase_noise_display")

    def read(self, operation, channel=1):
        if operation == "phase_noise_settings":
            self.check_pn()
            return snapshot(self)
        return super().read(operation, channel)

    def apply(self, command):
        if command.operation == "phase_noise_settings":
            return apply_settings(self, command)
        return super().apply(command)

    def validate_recorded_run(self, request):
        from .measurement import validate_request

        validate_request(request)

    def run_acquisition(self, request, stop):
        from .measurement import measure

        return measure(self, request, stop)

    def check_pn(self):
        if self.selected_application()[0] not in {"PNO", "PNOISE"} or self.query_token(
            "CONF:PNO:MEAS?"
        ) not in {"PNO", "PNOISE"}:
            raise DeviceError("请在仪器选择载波相位噪声测量。", "acquisition_setup_blocked")

    def selected_application(self):
        applications = self.read("applications")
        if len(applications) == 2:
            application = applications[0].upper()
            selected = applications[1].upper()
        else:
            selected = self.query_token("INST:SEL?")
            by_name = {
                name.upper(): kind.upper() for kind, name in zip(applications[::2], applications[1::2])
            }
            application = by_name.get(selected, selected)
        return application, selected

    def trace_settings(self):
        application, selected = self.selected_application()
        if application not in {"PNO", "PNOISE"}:
            raise DeviceError("请在面板选择相位噪声应用。", "acquisition_setup_blocked")
        empty = []
        for window in window_indices(self.transport.query("LAY:CAT?")):
            display = self.query_token(f"LAY:WIND{window}:TYPE?")
            if display not in LF_WINDOWS:
                continue
            for trace in TRACES:
                prefix = f"DISP:WIND{window}:TRAC{trace}"
                if self.query_token(prefix + ":STAT?") not in {"1", "ON"}:
                    continue
                if self.query_token(prefix + ":RES?") != "PN":
                    continue
                count = self.query_token(f"TRAC{window}:POIN? TRACE{trace}")
                if not count.lstrip("+").isdigit():
                    raise DeviceError("Invalid FSPN trace point count", "parse_error")
                points = int(count)
                if points == 0:
                    empty.append(f"窗口 {window} 迹线 {trace}")
                    continue
                if points > MAX_POINTS:
                    raise DeviceError(f"相噪曲线点数 {points} 超出单帧上限。", "parse_error")
                if self.query_token("CONF:PNO:MEAS?") not in {"PNO", "PNOISE"}:
                    raise DeviceError("请在面板选择载波相位噪声测量。", "acquisition_setup_blocked")
                transfer = self.read("transfer_format")
                if transfer not in {"ASC", "ASCII", "ASC,0", "ASCII,0"}:
                    raise DeviceError(
                        "当前曲线仅验证了 ASCII 传输，请在仪器上选择 ASCII。", "acquisition_setup_blocked"
                    )
                return selected, window, display, trace, points, transfer
        if empty:
            detail = "已启用的相噪曲线均为 0 点：" + "、".join(empty)
            for query, mask, reason in (
                ("STAT:QUES:PNO:COND?", 2, "仪器未找到载波，请检查 RF INPUT 的待测信号"),
                ("STAT:QUES:FREQ:COND?", 256, "仪器报告外部参考异常，请检查参考输入"),
            ):
                try:
                    condition = int(self.query_token(query))
                except (ValueError, DeviceError, OSError) as exc:
                    detail += f"。状态查询 {query} 失败（{exc}）"
                    break
                if condition & mask:
                    detail += "。" + reason
            raise DeviceError(detail, "waveform_unavailable")
        raise DeviceError("请在面板显示 L(f) 单边带相噪图并启用 PN 迹线。", "acquisition_setup_blocked")

    def acquire_measurements(self, request):
        if (
            request.kind != "trace"
            or request.initiate
            or request.channels != (1,)
            or request.quantity not in ("", "phase_noise")
        ):
            raise Unsupported("FSPN-26 supports a passive L(f) trace read")
        settings = self.trace_settings()
        _, window, display, trace, points, _ = settings
        raw = self.transport.query(f"TRAC{window}? TRACE{trace}")
        try:
            after = self.trace_settings()
        except DeviceError as exc:
            if exc.code not in {"acquisition_setup_blocked", "waveform_unavailable"}:
                raise
            raise DeviceError("相噪窗口或迹线在读取过程中失效。", "configuration_changed") from exc
        if settings != after:
            raise DeviceError("相噪窗口或迹线配置在读取过程中发生变化。", "configuration_changed")
        if not raw.strip():
            raise DeviceError("仪器尚未返回相噪曲线。", "waveform_unavailable")
        if raw.startswith(b"#"):
            raise DeviceError("FSPN trace format changed", "configuration_changed")
        values = numeric_values(raw)
        if values.size != points * 2:
            raise DeviceError("FSPN point count differs from trace data", "parse_error")
        axis, levels = values[::2], values[1::2]
        if (axis <= 0).any() or (np.diff(axis) <= 0).any() or (np.abs(values) >= 1e30).any():
            raise DeviceError("相噪曲线包含无效偏频或测量值。", "waveform_unavailable")
        return (
            Measurement(
                "phase_noise",
                "dBc/Hz",
                levels,
                1,
                axis,
                "Hz",
                metadata={
                    "window": window,
                    "window_type": display,
                    "trace": trace,
                    "points": points,
                    "source": "displayed_trace",
                },
            ),
        )
