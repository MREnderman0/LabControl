"""Role bar of the Moku role pages: every role's state at a glance, one click to switch.

Moku:Pro runs its signal generator, oscilloscope and spectrum analyzer at the same time in one
session. Each role keeps its own page; this bar shows the other roles on every page, so the
concurrent use and a shared physical input stay visible. Display only: it reads monitoring
snapshots and command receipts the UI already holds and sends nothing to the instrument.
"""

from __future__ import annotations

from PySide6.QtWidgets import QHBoxLayout, QWidget

from lab_control.domain.oscilloscope import engineering
from lab_control.ui.kit import DeviceChips, ElideLabel
from lab_control.ui.pages.monitoring.recording_state import recording_phase

from .adapter import KINDS, LABELS, ROLES

KIND_OF = {role: kind for kind, role in KINDS.items()}
RECEIPT_NOTE = "依据本会话的命令回执，不是实时读回；前面板或厂商上位机的改动不会显示在这里。"


def output_display(receipt: dict) -> tuple[str, str, str]:
    if receipt["state"] == "on":
        wave = f"正弦 {engineering(receipt['frequency'], 'Hz')} · {1000 * receipt['amplitude']:g} mVpp"
        return "输出中", "ok", wave
    if receipt["state"] == "off":
        return "已关闭", "neutral", "已确认关闭"
    return "状态待核实", "warn", "已尝试写入，最终输出状态待核实"


def generator_status(outputs: dict) -> tuple[str, str, str]:
    if not outputs:
        return "无输出回执", "idle", RECEIPT_NOTE
    channels: dict[str, list[str]] = {}
    details = []
    for channel, receipt in sorted(outputs.items()):
        label, _, detail = output_display(receipt)
        channels.setdefault(label, []).append(f"CH{channel}")
        details.append(f"CH{channel}  {detail}")
    parts = []
    for label in ("状态待核实", "输出中", "已关闭"):
        if label in channels:
            state = "输出" if label == "输出中" else label
            parts.append(f"{'、'.join(channels[label])} {state}")
    kind = "warn" if "状态待核实" in channels else "ok" if "输出中" in channels else "idle"
    return " · ".join(parts), kind, "\n".join([*details, RECEIPT_NOTE])


def role_recording(snapshot: dict, acquisition: str) -> dict:
    if acquisition in snapshot.get("recording_kinds", ()):
        return snapshot
    if not snapshot.get("recording") and acquisition in snapshot.get("recorder_kinds", ()):
        return {"recording_state": snapshot.get("recording_state", {})}
    return {}


class RoleBar(QWidget):
    """Chips for the three roles (status dot + short state) and a shared-input note."""

    def __init__(self, page) -> None:
        super().__init__(page)
        self.page = page
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(12)
        self.chips = DeviceChips(self)
        self.chips.setAccessibleName("功能角色")
        self.chips.changed.connect(self._switch)
        row.addWidget(self.chips)
        self.note = ElideLabel(self)
        self.note.setProperty("role", "caption")
        row.addWidget(self.note, 1)

    def items(self, snap: dict) -> tuple[list[tuple[str, str, str, str]], dict[str, int | None]]:
        page = self.page
        state = page.state
        connected = state.get("connectionState") in {"connected", "busy"}
        kinds = snap.get("kinds", ()) if snap.get("running") else ()
        items, channels = [], {}
        for role in ROLES:
            label = LABELS[role]
            if role == "signal_generator":
                status, kind, tip = generator_status(
                    state.get("capabilityDetails", {}).get("output_receipts", {})
                )
                text = f"{label} · {status}"
            else:
                acquisition = KIND_OF[role]
                frame = page.service.latest(page.device_id, acquisition) if acquisition in kinds else None
                channel = frame.request.channels[0] if frame is not None and frame.request.channels else None
                if channel is None and role == page.role and acquisition in kinds:
                    channel = page.channel  # Before the first frame: the channel this page started.
                channels[role] = channel
                where = f"ch{channel} " if channel else ""
                phase = recording_phase(role_recording(snap, acquisition))
                if phase == "preparing":
                    text, kind = f"{label} · {where}准备记录", "info"
                elif phase == "draining":
                    text, kind = f"{label} · 收尾中", "err"
                elif phase == "recording":
                    text, kind = f"{label} · {where}记录中", "err"
                elif acquisition in kinds:
                    text, kind = f"{label} · {where}采集中", "ok"
                else:
                    text, kind = f"{label} · 空闲", "idle"
                tip = f"{label}面板与其他功能同时运行，停止本面板不影响其他功能。"
            if not connected and kind != "warn":
                kind = "idle"
            items.append((text, role, kind, tip))
        return items, channels

    def refresh(self, snapshot: dict) -> None:
        items, channels = self.items(snapshot)
        self.chips.set_devices(items, self.page.role)
        scope, spectrum = channels.get("oscilloscope"), channels.get("spectrum_analyzer")
        note = (
            f"示波器与频谱仪共用 ch{scope} 输入前端：阻抗、耦合、衰减和带宽的修改对两者同时生效"
            if scope and scope == spectrum
            else ""
        )
        if self.note.text() != note:
            self.note.setText(note)
            self.note.setToolTip(note)

    def _switch(self, role: str) -> None:
        page = self.page
        # The target page selects its own chip; keep this one on the page's role.
        self.chips.set_current(page.role)
        navigate = getattr(page.window(), "navigate", None)
        if role != page.role and callable(navigate):
            navigate("仪器测控", role, page.device_id)
