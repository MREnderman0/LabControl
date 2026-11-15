"""Native phase-noise settings and verified partial updates (manual §§11.6.5–6)."""

import math

from lab_control.domain.instruments import ApplyResult, Capability, DeviceError, Parameter, ValidationError
from lab_control.instruments.base import CommandSpec, number

FIELDS = {
    "start_hz": ("SENS:FREQ:STAR", Parameter(unit="Hz", minimum=0)),
    "stop_hz": ("SENS:FREQ:STOP", Parameter(unit="Hz", minimum=0)),
    "rbw_percent": ("SENS:LIST:BWID:RAT", Parameter(unit="%", minimum=0.1, maximum=30)),
    "xcorr": ("SENS:SWE:XFAC", Parameter(minimum=1)),
    "attenuation_db": ("INP:ATT", Parameter(kind="integer", unit="dB", minimum=0, resolution=5)),
}
LABELS = {
    "start_hz": "起始频偏 (Hz)",
    "stop_hz": "终止频偏 (Hz)",
    "rbw_percent": "RBW (%)",
    "xcorr": "互相关因子",
    "attenuation_db": "输入衰减 (dB)",
}
SETTINGS = CommandSpec(
    Capability(
        "phase_noise_settings",
        {**{k: v[1] for k, v in FIELDS.items()}, "attenuation_auto": Parameter(kind="boolean")},
        readable=True,
        writable=True,
        partial_update="native",
        section="11.6.5–11.6.6",
    )
)


def value(adapter, command):
    result = number(adapter.transport.query(command + "?"))
    return result if abs(result) < 1e30 else None


def flag(adapter, command):
    result = adapter.query_token(command + "?")
    if result not in {"0", "+0", "1", "+1", "OFF", "ON"}:
        raise DeviceError(f"{command} 状态无法识别：{result}", "parse_error")
    return result in {"1", "+1", "ON"}


def snapshot(adapter):
    settings = {k: value(adapter, cmd) for k, (cmd, _) in FIELDS.items() if k != "attenuation_db"}
    auto = flag(adapter, "INP:ATT:AUTO")
    settings.update(
        attenuation_db=value(adapter, "INP:ATT"),
        attenuation_auto=auto,
        sweep_mode=adapter.query_token("SENS:SWE:MODE?"),
        carrier_missing=bool(int(adapter.query_token("STAT:QUES:PNO:COND?")) & 2),
        estimated_s=value(adapter, "SENS:SWE:TIME"),
    )
    for key, cmd in (("carrier_hz", "SENS:FREQ:CENT"), ("carrier_dbm", "SENS:POW:RLEV")):
        settings[key] = None if settings["carrier_missing"] else value(adapter, cmd)
    return settings


def apply_settings(adapter, command):
    _, changes = adapter.validate_command(command)
    adapter.check_pn()
    original = snapshot(adapter)
    target = original | changes
    if not changes or not (0 < target["start_hz"] < target["stop_hz"]):
        raise ValidationError("起止频偏必须大于零，且起始值小于终止值")
    if changes.keys() & {"rbw_percent", "xcorr"} and not original["sweep_mode"].startswith("NORM"):
        raise ValidationError("当前为分段手动配置，无法应用全局 RBW 或互相关因子")
    if "attenuation_db" in changes and (target["attenuation_auto"] or changes["attenuation_db"] % 5):
        raise ValidationError("手动输入衰减按 5 dB 调节，自动衰减开启时仅可查看")
    order = ["attenuation_auto", *FIELDS]
    if target["start_hz"] >= original["stop_hz"]:
        order[1:3] = ["stop_hz", "start_hz"]
    completed = 0
    actual = original.copy()
    for name in order:
        if name not in changes or changes[name] == original[name]:
            continue
        cmd = "INP:ATT:AUTO" if name == "attenuation_auto" else FIELDS[name][0]
        adapter.transport.write(f"{cmd} {changes[name]:.15g}")
        completed += 1
        actual[name] = value(adapter, cmd)
        error = adapter.query_token("SYST:ERR?")
        if (
            error.split(",")[0].strip() not in {"0", "+0"}
            or actual[name] is None
            or not math.isclose(actual[name], changes[name], rel_tol=1e-8, abs_tol=1e-12)
        ):
            return ApplyResult(
                "rejected",
                command.operation,
                changes,
                actual,
                completed,
                f"{LABELS.get(name, '自动衰减')} 请求 {changes[name]}，读回 {actual[name]}，仪器 {error}",
            )
    return ApplyResult("success", command.operation, changes, snapshot(adapter), completed)
