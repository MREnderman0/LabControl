from __future__ import annotations

from PySide6.QtCore import QSignalBlocker, QSize, Qt, Signal
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget

from lab_control.application.bulk_connection import device_label
from lab_control.application.catalog import instrument_category, surface_applicable
from lab_control.application.controls import CATEGORY_LABELS
from lab_control.domain.workspace import channel_label
from lab_control.ui.controller import WorkspaceController, display_connection_state, state_label
from lab_control.ui.kit import DeviceChips, icons, paint_device_card, style_button
from lab_control.ui.widgets.workspace_combo import WorkspaceComboBox

STATE_KIND = {
    "connected": "ok",
    "busy": "accent",
    "connecting": "warn",
    "recovering": "warn",
    "degraded": "err",
    "error": "err",
}


class DeviceCombo(WorkspaceComboBox):
    """The device choice drawn as a card (model, connection pill, vendor · serial · slot).

    Model, signals and popup are the normal WorkspaceComboBox; only the closed state is custom.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("deviceCombo")
        self.icon_name = "box"
        self.card: tuple[str, str, str, str] = ("", "", "", "neutral")
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumWidth(120)
        self.setMaximumWidth(420)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(54)

    def set_card(self, name: str, subtitle: str, state: str, kind: str) -> None:
        card = (name, subtitle, state, kind)
        if card != self.card:
            self.card = card
            self.setToolTip(f"{name}\n{subtitle}" if name else subtitle)
            self.update()

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(360, 54)

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return QSize(120, 54)

    def paintEvent(self, _event) -> None:  # noqa: N802
        name, subtitle, state, kind = self.card
        if not name:
            name = self.placeholderText() if self.currentIndex() < 0 else self.currentText()
        painter = QPainter(self)
        paint_device_card(
            painter,
            self.rect(),
            icon_name=self.icon_name,
            name=name,
            subtitle=subtitle,
            state=state,
            kind=kind,
            font=self.font(),
            hovered=self.underMouse() or self.hasFocus(),
            open_=self.view().isVisible(),
            enabled=self.isEnabled(),
        )


class DeviceSelector(QWidget):
    selected = Signal(str)
    channel_changed = Signal(int)

    def __init__(
        self,
        controller: WorkspaceController,
        category: str,
        parent=None,
        *,
        compact=False,
        surface: str | None = None,
    ) -> None:
        super().__init__(parent)
        self.controller, self.category, self.surface = controller, category, surface
        self.device_id = ""
        self._chip_count = 0
        self._names_key: tuple | None = None
        self.signature: tuple[tuple[str, str], ...] | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        row.setSpacing(10)
        self.combo, self.channel = DeviceCombo(), WorkspaceComboBox()
        self.combo.icon_name = icons.CATEGORY_ICONS.get(category, "box")
        self.combo.currentIndexChanged.connect(self._selected)
        self.channel.currentIndexChanged.connect(
            lambda: self.channel_changed.emit(self.channel.currentData() or 1)
        )
        row.addWidget(self.combo, 1)
        self.chips = DeviceChips(self)
        self.chips.changed.connect(self._chip_chosen)
        self.chips.hide()
        row.addWidget(self.chips, 0, Qt.AlignmentFlag.AlignVCenter)
        self.channel_label = channel_label_widget = QLabel("通道")
        channel_label_widget.setObjectName("mutedText")
        row.addWidget(channel_label_widget)
        row.addWidget(self.channel)
        self.connect_button = QPushButton("前往设备总览连接")
        style_button(self.connect_button, "ghost", "arrow")
        self.connect_button.clicked.connect(lambda: controller.navigate.emit("设备管理", "", ""))
        row.addWidget(self.connect_button)
        row.addStretch(0)
        layout.addLayout(row)
        self.info = QLabel()
        self.info.setWordWrap(True)
        self.info.setObjectName("mutedText")
        # The card shows identity, serial and slot; the text stays for tooltips and older tests.
        self.info.hide()
        layout.addWidget(self.info)
        if compact:
            channel_label_widget.hide()
            self.channel.hide()
        controller.refreshed.connect(self.refresh)
        self.refresh()

    def _records(self) -> tuple[dict, ...]:
        records = self.controller.connected_records(self.category)
        if self.category == "power_supply" and self.surface:
            return tuple(sorted(records, key=lambda r: not surface_applicable(r, self.surface)))
        return tuple(
            record for record in records if self.surface is None or surface_applicable(record, self.surface)
        )

    def refresh(self) -> None:
        records = self._records()
        entries = []
        chips = []
        slots = self.controller.runtime.repository.function_slots
        for entry in records:
            label = device_label(entry)
            slot = entry.get("slots", {}).get(self.controller.mode)
            connection = display_connection_state(self.controller.states[entry["id"]])
            suffix = " · " + state_label(connection) if connection != "connected" else ""
            slot_name = slots.label(slot, self.controller.mode)
            entries.append(((slot_name + " · " if slot else "") + label + suffix, entry["id"]))
            model = entry["identity"].get("model") or entry.get("adapterId", "")
            chips.append(
                (
                    slot_name if slot else model,
                    entry["id"],
                    STATE_KIND.get(connection, "idle"),
                    entries[-1][0],
                )
            )
        signature = tuple(entries)
        if signature != self.signature:
            initial = (
                self.signature is None
                and bool(records)
                and (not self.surface or surface_applicable(records[0], self.surface))
            )
            self.signature = signature
            self.combo.replace_items(entries, select_first=initial)
        self.combo.setPlaceholderText("选择已连接设备" if entries else "暂无已连接设备")
        self._chip_count = len(chips)
        if 1 < len(chips) <= 6:
            self.chips.set_devices(chips, self.device_id)
        record = self.controller.records.get(self.device_id)
        self.connect_button.setVisible(not bool(self.device_id))
        self.channel.setEnabled(bool(self.device_id))
        self._fit_chips()
        if record:
            key = (self.device_id, record.get("slots", {}).get(self.controller.mode), slots.names_revision)
            if key != self._names_key:
                self._names_key = key
                names = self.controller.channel_names(self.device_id)
                for index in range(self.channel.count()):
                    self.channel.setItemText(index, channel_label(names, self.channel.itemData(index)))
            identity = record["identity"]
            serial = identity.get("serial") or record["config"].get("expected_serial") or "未识别"
            serial_label = (
                "序列号"
                if identity.get("serial")
                else "资产序列号（配置）"
                if record["config"].get("expected_serial")
                else "序列号"
            )
            model = identity.get("model") or record["adapterId"]
            self.info.setText(
                f"{model}  ·  {record['config']['resource']}\n"
                f"{serial_label}：{serial}  ·  "
                + ("待实机验收" if self.controller.runtime.hardware else "模拟设备")
            )
            slot = record.get("slots", {}).get(self.controller.mode)
            state = self.controller.states.get(self.device_id, {})
            connection = display_connection_state(state)
            name = record.get("displayName") or ""
            subtitle = " · ".join(
                value
                for value in (
                    identity.get("manufacturer") or "",
                    name if name and name != model else "",
                    serial if serial != "未识别" else record["config"]["resource"],
                    slots.label(slot, self.controller.mode) if slot else "槽位未绑定",
                )
                if value
            )
            busy = state.get("activeTask") == "continuous_acquisition"
            self.combo.set_card(
                model,
                subtitle,
                "采集中" if busy and connection == "connected" else state_label(connection),
                "accent" if busy else STATE_KIND.get(connection, "neutral"),
            )
            self.combo.setToolTip(self.info.text())
        else:
            text = f"请先在设备总览连接{CATEGORY_LABELS[self.category]}，再选择设备。"
            self.info.setText(text)
            self.combo.set_card(
                "选择已连接设备" if records else "暂无已连接设备",
                text if not records else f"{len(records)} 台{CATEGORY_LABELS[self.category]}可选",
                "",
                "neutral",
            )

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._fit_chips()

    def _fit_chips(self) -> None:
        width = self.chips.sizeHint().width()
        reserved = 190 + sum(
            w.sizeHint().width() + 10
            for w in (self.channel_label, self.channel, self.connect_button)
            if not w.isHidden()
        )
        visible = 1 < self._chip_count <= 6 and 0 < width <= self.width() - reserved
        self.chips.setMaximumWidth(width if visible else 0)
        self.chips.setVisible(visible)

    def _chip_chosen(self, device_id) -> None:
        if device_id and device_id != self.device_id:
            index = self.combo.findData(device_id)
            if index >= 0:
                self.combo.setCurrentIndex(index)

    def _selected(self) -> None:
        device_id = self.combo.currentData() or ""
        if device_id == self.device_id:
            return
        record = self.controller.records.get(device_id)
        if self.surface and record and not surface_applicable(record, self.surface):
            with QSignalBlocker(self.combo):
                self.combo.setCurrentIndex(self.combo.findData(self.device_id))
            self.controller.navigate.emit("仪器测控", self.category, device_id)
            return
        self.device_id = device_id
        self._names_key = None
        self.channel.blockSignals(True)
        self.channel.clear()
        if device_id:
            names = self.controller.channel_names(device_id)
            for channel in self.controller.capabilities(device_id).channels:
                self.channel.addItem(channel_label(names, channel), channel)
            category = instrument_category(self.controller.records[device_id])
            self.combo.icon_name = icons.CATEGORY_ICONS.get(category, self.combo.icon_name)
        self.channel.blockSignals(False)
        self.selected.emit(device_id)
        self.refresh()

    def select(self, device_id: str, channel: int | None = None) -> None:
        self.refresh()
        index = self.combo.findData(device_id)
        if index >= 0:
            self.combo.setCurrentIndex(index)
            if channel is not None:
                index = self.channel.findData(channel)
                if index >= 0:
                    self.channel.setCurrentIndex(index)
        else:
            # A stale navigation target must never leave a different device selected.
            self.combo.setCurrentIndex(-1)
            self._selected()
        self.refresh()
