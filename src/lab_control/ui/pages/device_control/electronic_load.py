"""Electronic load management: one passive readback table and one channel editor."""

from typing import Any

from PySide6.QtCore import Qt, QTime, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from lab_control.application.monitoring_state import MonitorFrame
from lab_control.domain.instruments import AcquisitionRequest, ParameterReading
from lab_control.ui.controller import WorkspaceController
from lab_control.ui.kit import style_button
from lab_control.ui.pages.device_control.form import ControlForm, OperationEditor
from lab_control.ui.pages.device_control.signal_source import ParameterGroup
from lab_control.ui.widgets.device_selector import DeviceSelector

MODE_TARGETS = {
    "CC": ("current", "A"),
    "CV": ("voltage", "V"),
    "CP": ("load_power", "W"),
    "CR": ("resistance", "Ω"),
}


class LoadSettings(ParameterGroup):
    finished = Signal()

    def __init__(self, controller, key, channel, caps):
        super().__init__(controller, key, channel, caps, columns=3, simplified=True)
        layout = self.layout()
        assert layout is not None
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self.fields.setColumnStretch(2, 1)
        titles = {
            "load_input": "拉载输入",
            "load_mode": "工作模式",
            "current": "恒流设定",
            "voltage": "恒压设定",
            "load_power": "恒功率设定",
            "resistance": "恒阻设定",
        }
        for name, editor in self.editors.items():
            title = editor.findChild(QLabel, "controlTitle")
            if title is not None:
                title.setText(titles[name])
            editor.auto_sync = False  # This panel receives one explicit snapshot shared by all channels.
            editor.editors["value"].changed.connect(self.refresh)
        mode_box = self.editors["load_mode"].editors["value"].input
        assert isinstance(mode_box, QComboBox)
        self.mode_box = mode_box
        descriptions = {"CC": "恒流", "CV": "恒压", "CP": "恒功率", "CR": "恒阻"}
        for index in range(self.mode_box.count()):
            value = self.mode_box.itemData(index)
            self.mode_box.setItemText(index, f"{value} · {descriptions[value]}")
        self.mode_box.currentIndexChanged.connect(self.show_target)
        self.show_target()

    def command_order(self, item):
        name, params = item
        if name == "load_input":
            return 3 if params["value"] else 0
        return 1 if name == "load_mode" else 2

    def show_target(self):
        target = MODE_TARGETS.get(self.mode_box.currentData(), ("", ""))[0]
        self.context_hidden = {name for name, _ in MODE_TARGETS.values() if name != target}
        for name, editor in self.editors.items():
            editor.setVisible(name not in self.context_hidden)
        self.visible_columns = 0
        self.reflow_fields()
        self.refresh()

    def refresh(self):
        super().refresh()
        dirty = sum(e.dirty for name, e in self.editors.items() if name not in self.context_hidden)
        self.count.setText(f"{dirty} 项待应用" if dirty else "")
        if self.running:
            self.count.setText("正在应用…")

    def finish(self, reason, *, failed=None):
        super().finish(reason, failed=failed)
        self.finished.emit()


class ElectronicLoadPage(QWidget):
    def __init__(self, controller: WorkspaceController, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.device_id = ""
        self.session_id: str | None = None
        self.revision = 0
        self.reading = False
        self.frame: MonitorFrame | None = None
        self.settings: dict[int, dict[str, Any]] = {}
        self.editors: dict[str, OperationEditor] = {}
        self.fallback: ControlForm | None = None
        self.channels: tuple[int, ...] = ()
        self.channel: int | None = None
        self.mode = controller.mode

        self.page_layout = layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(8)
        header = QHBoxLayout()
        self.selector = DeviceSelector(
            controller, "electronic_load", self, compact=True, surface="management"
        )
        header.addWidget(self.selector, 1)
        self.refresh_button = QPushButton("刷新读数")
        style_button(self.refresh_button, "", "refresh")
        self.refresh_button.setProperty("buttonSize", "compact")
        self.refresh_button.clicked.connect(self.read_snapshot)
        header.addWidget(self.refresh_button)
        self.save_button = QPushButton("保存当前读数")
        style_button(self.save_button, "", "download")
        self.save_button.setProperty("buttonSize", "compact")
        self.save_button.clicked.connect(self.save)
        header.addWidget(self.save_button)
        layout.addLayout(header)

        self.table = QTableWidget(0, 6)
        self.table.setObjectName("loadChannels")
        self.table.setHorizontalHeaderLabels(
            ("通道", "输入", "模式", "设定值", "实测电压 (V)", "实测电流 (A)")
        )
        for col in range(3, 6):
            header_item = self.table.horizontalHeaderItem(col)
            assert header_item is not None
            header_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(34)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setAccessibleName("负载通道读数")
        layout.addWidget(self.table)

        self.panel = QFrame()
        self.panel.setObjectName("operationCard")
        card = QVBoxLayout(self.panel)
        self.title = QLabel()
        self.title.setObjectName("spectrumChartTitle")
        card.addWidget(self.title)
        self.fields = QHBoxLayout()
        self.fields.setSpacing(18)
        card.addLayout(self.fields)
        layout.addWidget(self.panel)
        self.note = QLabel()
        self.note.setObjectName("mutedText")
        self.note.setWordWrap(True)
        layout.addWidget(self.note)
        self.tail_spacer = QWidget()
        layout.addWidget(self.tail_spacer, 1)

        self.selector.selected.connect(self.bind)
        self.selector.channel_changed.connect(self.select_channel)
        self.table.currentCellChanged.connect(self.row_changed)
        controller.refreshed.connect(self.sync)
        self.bind(self.selector.device_id)

    def bind(self, device_id):
        if self.device_id in self.controller.group_devices and self.device_id != device_id:
            self.selector.select(self.device_id, self.channel or 1)
            return
        self.device_id = device_id
        self.session_id = self.controller.states.get(device_id, {}).get("sessionId")
        self.mode = self.controller.mode
        self.revision += 1
        self.reading = False
        self.frame = None
        self.settings = {}
        self.save_button.setEnabled(False)
        self.refresh_button.setEnabled(bool(device_id))
        self.note.setText("" if device_id else "请先连接电子负载。")
        self.channels = tuple(self.controller.capabilities(device_id).channels) if device_id else ()
        generic = bool(
            device_id
            and not any(cap.name == "load_mode" for cap in self.controller.capabilities(device_id).operations)
        )
        if generic and self.fallback is None:
            self.fallback = ControlForm(self.controller)
            self.page_layout.insertWidget(self.page_layout.indexOf(self.tail_spacer), self.fallback, 1)
        if self.fallback:
            self.fallback.setVisible(generic)
            self.fallback.set_device(device_id if generic else "")
        self.tail_spacer.setVisible(not generic)
        self.note.setVisible(not generic or not device_id)
        self.selector.channel.setVisible(generic)
        self.table.setVisible(not generic)
        self.refresh_button.setVisible(not generic)
        self.save_button.setVisible(not generic)
        if generic:
            self.panel.hide()
            return
        self.table.blockSignals(True)
        self.table.setRowCount(len(self.channels))
        for row, channel in enumerate(self.channels):
            for col in range(self.table.columnCount()):
                item = QTableWidgetItem(f"CH {channel}" if col == 0 else "—")
                item.setTextAlignment(
                    Qt.AlignmentFlag.AlignVCenter
                    | (Qt.AlignmentFlag.AlignRight if col >= 3 else Qt.AlignmentFlag.AlignCenter)
                )
                self.table.setItem(row, col, item)
        self.table.blockSignals(False)
        self.table.ensurePolished()
        self.table.horizontalHeader().ensurePolished()
        self.table.setFixedHeight(
            self.table.horizontalHeader().sizeHint().height()
            + sum(self.table.rowHeight(i) for i in range(len(self.channels)))
            + 2 * self.table.frameWidth()
            + 2
        )
        self.channel = None
        self.select_channel(self.selector.channel.currentData())
        if self.isVisible() and device_id:
            self.read_snapshot()

    def sync(self):
        state = self.controller.states.get(self.device_id, {})
        if self.session_id != state.get("sessionId") or self.mode != self.controller.mode:
            self.bind(self.selector.device_id)

    def row_changed(self, row, _col, _old_row, _old_col):
        if 0 <= row < len(self.channels):
            self.selector.channel.setCurrentIndex(self.selector.channel.findData(self.channels[row]))

    def select_channel(self, channel):
        if (
            self.device_id in self.controller.group_devices
            and self.channel in self.channels
            and self.channel != channel
        ):
            self.selector.channel.setCurrentIndex(self.selector.channel.findData(self.channel))
            self.table.selectRow(self.channels.index(self.channel))
            return
        if self.fallback and not self.fallback.isHidden():
            self.fallback.set_device(self.device_id, channel or 1)
            return
        if channel == self.channel and self.editors:
            return
        self.channel = channel
        while self.fields.count():
            item = self.fields.takeAt(0)
            widget = item.widget() if item else None
            if widget is not None:
                widget.hide()
                widget.deleteLater()
        self.editors = {}
        self.panel.setVisible(channel in self.channels)
        if channel not in self.channels:
            return
        self.table.selectRow(self.channels.index(channel))
        self.title.setText(f"CH {channel} · 通道设置")
        schema = self.controller.capabilities(self.device_id, channel)
        caps = [cap for cap in schema.operations if cap.writable]
        self.group = LoadSettings(self.controller, self.device_id, channel, caps)
        self.group.refresh()
        self.group.finished.connect(self.schedule_refresh)
        self.fields.addWidget(self.group)
        self.editors = self.group.editors
        self.update_fields()

    def update_fields(self):
        if self.channel is None or not self.editors:
            return
        values = self.settings.get(self.channel, {})
        for name, editor in self.editors.items():
            if not editor.busy and name in values:
                editor.show_reading(ParameterReading(name, self.channel, values[name], "instrument"))
                editor.readback.hide()
        self.group.show_target()

    def schedule_refresh(self):
        if self.isVisible():
            # Any older in-flight snapshot belongs to the configuration before this apply.
            self.revision += 1
            self.reading = False
            self.read_snapshot()

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self, self.read_snapshot)

    def read_snapshot(self):
        if not self.device_id or self.reading or not self.isVisible() or self.table.isHidden():
            return
        key, token = self.device_id, self.revision
        self.reading = True
        self.refresh_button.setEnabled(False)
        self.save_button.setEnabled(False)
        self.frame = None
        self.note.setText("正在读取…")
        sessions = self.controller.runtime.sessions

        def failed(error):
            if token == self.revision:
                self.reading = False
                self.refresh_button.setEnabled(True)
                self.settings = {}
                self.frame = None
                self.save_button.setEnabled(False)
                self.render_table({})
                self.note.setText(f"读取失败 · {error}")

        def got_status(status):
            if token != self.revision or self.session_id != self.controller.states.get(key, {}).get(
                "sessionId"
            ):
                return
            channels = tuple(status.values["selected_channels"])
            request = AcquisitionRequest("scalar", channels, points=1, allow_setup=False)

            def got_batch(batch):
                if (
                    self.session_id is None
                    or token != self.revision
                    or self.session_id != self.controller.states.get(key, {}).get("sessionId")
                ):
                    return
                self.settings = {int(ch): fields for ch, fields in status.values["channels"].items()}
                measured = {(m.channel, m.quantity): m.values[0] for m in batch.measurements}
                self.render_table(measured)
                self.update_fields()
                self.frame = MonitorFrame(
                    0,
                    self.controller.states[key]["configurationVersion"],
                    request,
                    batch,
                    session_id=self.session_id,
                )
                self.reading = False
                self.refresh_button.setEnabled(True)
                self.save_button.setEnabled(True)
                self.note.setText(
                    f"已更新 {QTime.currentTime().toString('HH:mm:ss')} · 共 {len(channels)} 路"
                    if len(channels) == len(self.channels)
                    else "面板当前只返回 " + "、".join(f"CH {ch}" for ch in channels)
                )

            self.controller.watch(sessions.submit(key, "acquire", request), got_batch, failed)

        try:
            self.controller.watch(sessions.submit(key, "status"), got_status, failed)
        except Exception as exc:
            failed(exc)

    def render_table(self, measured):
        for row, ch in enumerate(self.channels):
            data = self.settings.get(ch, {})
            mode = data.get("load_mode", "—")
            target, unit = MODE_TARGETS.get(mode, ("", ""))
            load_input = data.get("load_input")
            values = [
                ("开启" if load_input else "关闭") if isinstance(load_input, bool) else "—",
                mode,
                f"{data[target]:g} {unit}" if target in data else "—",
                *[f"{measured[ch, q]:g}" if (ch, q) in measured else "—" for q in ("voltage", "current")],
            ]
            for col, value in enumerate(values, 1):
                item = self.table.item(row, col)
                if item is not None:
                    item.setText(value)

    def save(self):
        if self.frame:
            self.controller.save_displayed_frame(
                self,
                self.device_id,
                self.frame,
                self.controller.mode,
                on_saved=lambda path: self.controller.message.emit(f"读数已保存 · {path}"),
            )
