"""Asset-bound four-wire Pt100 inputs on 7708 cards."""

from dataclasses import dataclass

from lab_control.domain.instruments import DeviceError, ValidationError

from .protocol import channel_list


@dataclass(frozen=True)
class Pt100Inputs:
    serial: str
    cards: dict[str, str]
    channels: tuple[int, ...]

    @classmethod
    def from_state(cls, data):
        if not isinstance(data, dict):
            raise ValidationError("Invalid 2701 Pt100 input configuration")
        serial = data.get("serial")
        cards = data.get("cards")
        channels = data.get("channels")
        if not isinstance(serial, str) or not serial:
            raise ValidationError("Missing 2701 serial for Pt100 inputs")
        if (
            not isinstance(channels, list)
            or not channels
            or any(
                type(ch) is not int or ch // 100 not in (1, 2) or not 1 <= ch % 100 <= 20 for ch in channels
            )
            or len(set(channels)) != len(channels)
        ):
            raise ValidationError("Select distinct four-wire primary inputs 101–120 or 201–220")
        if (
            not isinstance(cards, dict)
            or set(cards) != {str(ch // 100) for ch in channels}
            or not all(isinstance(v, str) and v.isdecimal() and int(v) for v in cards.values())
        ):
            raise ValidationError("Missing physical 7708 serial for a selected input")
        return cls(serial, dict(cards), tuple(channels))

    def verified_channels(self, serial, slots, scan):
        if self.serial != serial or any(slots.get(int(k)) != v for k, v in self.cards.items()):
            raise DeviceError("Pt100 测点绑定的主机或 7708 卡已变化", "identity_conflict")
        if not set(self.channels).issubset(scan):
            raise DeviceError("Pt100 测点已移出仪器扫描列表", "configuration_changed")
        return self.channels


# Restore in reverse order, while the meter is still in TEMP mode.
SETUP = (("FUNC", '"TEMP"'), ("TEMP:TRAN", "FRTD"), ("TEMP:FRTD:TYPE", "PT100"), ("UNIT:TEMP", "C"))


def validate_pair(channel, system, closed):
    pair = {channel, channel + 20}
    inputs = {ch for ch in channel_list(closed) if 1 <= ch % 100 <= 40}
    if set(channel_list(system)) not in ({channel}, pair) or inputs != pair:
        raise DeviceError(f"7708 四线通道 {channel} 的测量或感测继电器不匹配", "readback_mismatch")
