"""MokuOS 4.4 measurement-slot commands and their documented response fields."""

from math import isclose, isfinite

from lab_control.domain.instruments import ApplyResult, Capability, DeviceError, Parameter, ValidationError


def choice(*values):
    return Parameter("enum", choices=tuple(v.upper() for v in values))


WINDOWS = (
    "Rectangular",
    "Bartlett",
    "Hamming",
    "Hann",
    "BlackmanHarris",
    "FlatTop",
    "Nuttall",
    "Gaussian",
    "Kaiser",
)
TOKENS = {
    s.upper(): s
    for s in (
        *WINDOWS,
        "Linear",
        "SinX",
        "Normal",
        "Precision",
        "Auto",
        "Manual",
        "Minimum",
        "Rising",
        "Falling",
        "Both",
        "Current",
        "External",
    )
}
FREQUENCY = Parameter(unit="Hz", minimum=0, maximum=600e6)
CAPTURE = {
    "scope_timebase": (
        "timebase",
        {
            "t1": Parameter(unit="s"),
            "t2": Parameter(unit="s"),
            "max_length": Parameter("integer", choices=tuple(str(2**n) for n in range(7, 15))),
        },
    ),
    "scope_trigger": (
        "trigger",
        {
            "source": choice("Current", "External"),
            "mode": choice("Auto", "Normal"),
            "edge": choice("Rising", "Falling", "Both"),
            "level": Parameter(unit="V", minimum=-20, maximum=20),
        },
    ),
    "scope_acquisition": ("acquisition_mode", {"mode": choice("Normal", "Precision")}),
    "scope_interpolation": ("interpolation", {"interpolation": choice("Linear", "SinX", "Gaussian")}),
    "spectrum_span": ("span", {"frequency1": FREQUENCY, "frequency2": FREQUENCY}),
    "spectrum_rbw": (
        "rbw",
        {
            "mode": choice("Auto", "Manual", "Minimum"),
            "rbw_value": Parameter(unit="Hz", minimum=0, required=False),
        },
    ),
    "spectrum_window": ("window", {"window": choice(*WINDOWS)}),
    "spectrum_averaging": ("averaging", {"target_duration": Parameter(unit="s", minimum=0)}),
}
ACK_ONLY = {"trigger", "averaging"}
ALIASES = {
    "t1": "timebase1",
    "t2": "timebase2",
    "rbw_value": "value",
    "source": "Trigger channel",
    "mode": "Trigger mode",
    "type": "Trigger type",
    "edge": "Trigger edge",
}


def capture_capabilities():
    for name, (_, fields) in CAPTURE.items():
        yield Capability(
            name, fields, writable=True, requires_confirmation=True, channel_scoped=name == "scope_trigger"
        )


def field(data, key):
    if isinstance(data, dict):
        return data.get(key, data.get(ALIASES.get(key)))
    return None


def number(value):
    return type(value) in (int, float) and isfinite(value)


def matches(actual, expected):
    if type(expected) in (int, float):
        return number(actual) and isclose(actual, expected, rel_tol=1e-9, abs_tol=1e-15)
    return actual == expected


def wire_parameters(name, params):
    method, _ = CAPTURE[name]
    body = {k: TOKENS[v] if isinstance(v, str) else v for k, v in params.items()}
    if method == "timebase" and body["t1"] >= body["t2"]:
        raise ValidationError("时窗终点必须晚于起点")
    if method == "span" and body["frequency1"] >= body["frequency2"]:
        raise ValidationError("终止频率必须大于起始频率")
    if method == "rbw":
        if body["mode"] == "Manual" and body.get("rbw_value", 0) <= 0:
            raise ValidationError("手动 RBW 需要正的带宽值")
        if body["mode"] != "Manual" and "rbw_value" in body:
            raise ValidationError("仅手动模式接受 RBW 数值")
    if method == "averaging" and body["target_duration"] <= 0:
        raise ValidationError("平均帧时长必须大于零")
    return body


def apply_capture(adapter, name, params, body, channel):
    method, _ = CAPTURE[name]
    role = "oscilloscope" if name.startswith("scope_") else "spectrum_analyzer"
    if method == "span" and body["frequency2"] > 300e6:
        if adapter._frontend(channel).get("bandwidth") != "600MHz":
            raise ValidationError(f"频段超过 300 MHz，请先将 CH {channel} 的共享模拟前端带宽设为 600 MHz")
    if method == "trigger":
        body["type"] = "Edge"
        if body["source"] == "Current":
            logical = adapter._input(role, channel, adapter._routes())
            body["source"] = "Channel" + "ABCD"[logical - 1]
    ack = adapter._slot(role, "POST", "set_" + method, {"strict": True, **body})
    if method in ACK_ONLY:
        actual = {key: field(ack, key) for key in body}
        if not all(matches(actual[k], v) for k, v in body.items() if k != "level"):
            raise DeviceError("设置回执与请求不符，请核对仪器状态")
        if method == "trigger" and not (number(actual["level"]) and -20 <= actual["level"] <= 20):
            raise DeviceError("触发电平回执无效")
        return ApplyResult("success", name, params, actual, 1, acknowledged=True)

    read = adapter._slot(role, "GET", "get_" + method)
    # get_timebase returns endpoints only. get_rbw reports the quantized bandwidth.
    actual = {}
    for key, requested in body.items():
        if key in ("max_length", "rbw_value"):
            continue
        value = field(read, key)
        echoed = field(ack, key)
        if method == "rbw" and key == "mode" and value is None and echoed == requested:
            # Older envelopes return just the bandwidth. Only report fields independently read.
            continue
        if key == "window":
            value = "Hann" if value == "Hanning" else value
            echoed = "Hann" if echoed == "Hanning" else echoed
        if not matches(value, echoed) or not (
            number(value) if method in {"timebase", "span"} else matches(value, requested)
        ):
            raise DeviceError("参数读回与设置不符，请核对仪器状态")
        actual[key] = value
    if method == "timebase" and actual["t1"] >= actual["t2"]:
        raise DeviceError("时窗读回无效")
    if method == "span" and not 0 <= actual["frequency1"] < actual["frequency2"] <= FREQUENCY.maximum:
        raise DeviceError("频段读回无效")
    if method == "rbw":
        value = field(read, "rbw_value")
        if not number(value) or value <= 0:
            raise DeviceError("未读回有效的实际 RBW")
        actual["rbw_value"] = value
    return ApplyResult("success", name, params, actual, 1)
