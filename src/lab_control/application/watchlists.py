"""Workstation watch presets and the numeric signals advertised by connected models."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field, replace
from uuid import uuid4

from lab_control.application.config import config_from_record
from lab_control.application.controls import DataSource, data_sources
from lab_control.domain.instruments import ValidationError
from lab_control.domain.metrics import MetricProfile
from lab_control.instruments.registry import DEVICE_DEFINITIONS


def new_id():
    return uuid4().hex


@dataclass(frozen=True)
class WatchRow:
    device: str
    slot: str
    adapter: str
    channel: int
    source: str
    label: str
    profile: str = ""
    version: str = ""
    metric: str = ""
    parameters: dict = field(default_factory=dict)
    quantity: str = ""
    unit: str = ""
    lower: float | None = None
    upper: float | None = None
    id: str = field(default_factory=new_id)


@dataclass(frozen=True)
class Preset:
    name: str
    rows: tuple[WatchRow, ...] = ()
    id: str = field(default_factory=new_id)
    pinned: bool = False


class Presets:
    key = "watchlists/v1"

    def __init__(self, repository):
        self.repository = repository
        self.items = tuple(
            Preset(p["name"], tuple(WatchRow(**r) for r in p["rows"]), p["id"], p.get("pinned", False))
            for p in repository.load_workspace(self.key, [])
        )

    @property
    def pinned(self) -> Preset | None:
        return next((preset for preset in self.items if preset.pinned), None)

    def pin(self, key):
        if key and self.get(key) is None:
            raise ValidationError("监视预设不存在")
        self.save(replace(p, pinned=p.id == key) for p in self.items)

    def save(self, items):
        items = tuple(items)
        if sum(p.pinned for p in items) > 1:
            raise ValidationError("首页只能置顶一个监视预设")
        for p in items:
            if not p.name.strip():
                raise ValidationError("请输入预设名称")
            for row in p.rows:
                if any(v is not None and not math.isfinite(v) for v in (row.lower, row.upper)):
                    raise ValidationError("范围须为有限数值")
                if row.lower is not None and row.upper is not None and row.lower > row.upper:
                    raise ValidationError("下限不能大于上限")
        self.repository.save_workspace(self.key, [asdict(p) for p in items])
        self.items = items

    def get(self, key):
        return next((p for p in self.items if p.id == key), None)

    def update(self, key, **changes):
        self.save(replace(p, **changes) if p.id == key else p for p in self.items)


@dataclass(frozen=True)
class SignalSpec:
    source: DataSource
    profile: MetricProfile | None
    metric: str
    label: str
    unit: str
    quantity: str

    @property
    def key(self):
        return self.source.key, self.profile.id if self.profile else "", self.metric

    def row(self, record, channel, parameters):
        return WatchRow(
            record["id"],
            record.get("slots", {}).get("general", ""),
            record["adapterId"],
            channel,
            self.source.key,
            self.label,
            self.key[1],
            self.profile.version if self.profile else "",
            self.metric,
            parameters,
            self.quantity,
            self.unit,
        )


def signal_catalog(record, snapshot):
    if snapshot is None:
        return (), {}
    config = config_from_record(record)
    definition = DEVICE_DEFINITIONS.get(config.adapter)
    channels = tuple(snapshot.details.get("acquisition_channels", snapshot.channels))
    sources = {s.key: s for s in data_sources(config)}
    specs = {}
    for s in sources.values():
        if s.kind != "scalar":
            continue
        quantity = s.measurement or s.quantity
        unit = {"voltage": "V", "current": "A", "frequency": "Hz"}.get(quantity, "")
        if s.key == "reading":
            quantity = snapshot.details.get("reading_quantity", quantity)
            unit = snapshot.details.get("reading_unit", unit)
        spec = SignalSpec(s, None, "", "温度" if quantity == "temperature" else s.label, unit, quantity)
        specs[spec.key] = spec
    profiles = definition.metric_profiles + definition.watch_only_profiles if definition else ()
    for profile in profiles:
        source = next((sources[k] for k in profile.watch_sources if k in sources), None)
        if source:
            for metric in profile.metrics:
                label = {
                    "mean": "平均值",
                    "peak_to_peak": "峰峰值",
                    "rms": "RMS",
                    "std": "标准差",
                    "peak_frequency": "主峰频率",
                    "peak_power": "主峰功率",
                    "snr": "信噪比",
                    "spur_rejection": "杂散抑制比",
                    "phase_noise": "指定偏频处相噪",
                    "fluctuation_percent": "光功率波动",
                    "min_deviation_percent": "最小相对偏差",
                    "max_deviation_percent": "最大相对偏差",
                }.get(metric.name, metric.description)
                if profile.supports_multiple_recordings:
                    label += "（30 min）"
                spec = SignalSpec(source, profile, metric.name, label, metric.unit, "")
                specs[spec.key] = spec
    return channels, specs


def bounds_status(value, row):
    if row.lower is not None and value < row.lower:
        return "低于下限"
    if row.upper is not None and value > row.upper:
        return "高于上限"
    return "正常"
