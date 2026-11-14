"""Edit workstation-local names on the current binding, without instrument I/O."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from lab_control.application.config import config_from_record
from lab_control.domain.instruments import ValidationError
from lab_control.instruments.registry import DEVICE_DEFINITIONS

if TYPE_CHECKING:
    from lab_control.application.runtime import ApplicationRuntime


@dataclass(frozen=True)
class ChannelBinding:
    device_id: str
    mode: str
    slot: str
    revision: str
    channels: tuple[int, ...]


def current_binding(runtime: ApplicationRuntime, device_id: str, mode: str) -> ChannelBinding:
    with runtime.sessions.lock, runtime.inventory.lock:
        record = runtime.inventory.records.get(device_id, {})
        definition = DEVICE_DEFINITIONS.get(record.get("adapterId", ""))
        if definition is None or definition.nameable_channels is None:
            raise ValidationError("当前设备不支持编辑通道名")
        slot = record.get("slots", {}).get(mode, "")
        if not slot:
            raise ValidationError("请先绑定功能槽位")
        snap = runtime.sessions.capability_snapshot(device_id)
        if snap and snap.details.get("acquisition_channel_selection") == "scan_list":
            channels = tuple(snap.details.get("acquisition_channels", ()))
        elif snap:
            channels = tuple(snap.channels)
        else:
            channels = definition.nameable_channels(config_from_record(record))
        return ChannelBinding(
            device_id, mode, slot, record.get("slotBindingRevision", {}).get(mode, ""), channels
        )


def save_names(runtime: ApplicationRuntime, binding: ChannelBinding, edits: dict[int, str] | None) -> None:
    """None resets the entire slot. Ordinary edits preserve channels absent from this binding."""
    with runtime.sessions.lock, runtime.inventory.lock:
        try:
            current = current_binding(runtime, binding.device_id, binding.mode)
        except ValidationError:
            current = None
        if current != binding:
            raise ValidationError("设备绑定或通道已变化，请重新打开编辑器")
        slots = runtime.repository.function_slots
        names = slots.channel_names(binding.slot, binding.mode) if edits is not None else {}
        for channel, text in (edits or {}).items():
            if channel not in binding.channels:
                raise ValidationError("当前设备没有此通道")
            name = text.strip()
            if len(name) > 64 or any(ord(c) < 32 or ord(c) == 127 for c in name):
                raise ValidationError("通道名最多 64 个字符，不能包含控制字符")
            if name:
                names[str(channel)] = name
            else:
                names.pop(str(channel), None)
        slots.save_channel_names(binding.slot, binding.mode, names)
