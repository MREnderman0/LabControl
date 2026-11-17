"""Editable table shared by the main watchlist and its modeless windows."""

from dataclasses import replace
from datetime import datetime

from PySide6.QtCore import QAbstractTableModel, QMimeData, QModelIndex, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from lab_control.application.watchlists import WatchRow
from lab_control.domain.workspace import channel_label
from lab_control.ui.pages.watch_picker import WatchPicker


class WatchTableModel(QAbstractTableModel):
    columns = ("仪器／槽位", "通道／测点", "监视量", "当前值", "单位", "下限", "上限", "更新时间", "状态")
    mime_type = "application/x-tflab-watch-rows"

    def __init__(self, hub, parent):
        super().__init__(parent)
        self.hub = hub
        self.preset = ""
        self.rows: tuple[WatchRow, ...] = ()
        hub.changed.connect(self.reload)
        hub.updated.connect(self.refresh)

    def bind(self, preset):
        self.preset = preset
        self.reload()

    def reload(self):
        p = self.hub.presets.get(self.preset)
        rows = p.rows if p else ()
        if rows != self.rows:
            self.beginResetModel()
            self.rows = rows
            self.endResetModel()

    def refresh(self):
        if self.rows:
            self.dataChanged.emit(
                self.index(0, 0),
                self.index(len(self.rows) - 1, 8),
                [Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ForegroundRole],
            )

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return len(self.columns)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole:
            return self.columns[section] if orientation == Qt.Orientation.Horizontal else section + 1
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = self.rows[index.row()]
        col = index.column()
        status = self.hub.status.get(row.id, "等待读数")
        reading = self.hub.values.get(row.id, ("", None))[1]
        if role == Qt.ItemDataRole.ForegroundRole and col in (3, 8):
            if status in {"高于上限", "低于下限"}:
                return QColor("#ef6661")
            if status != "正常":
                return QColor("#9aa7ac")
        if role == Qt.ItemDataRole.TextAlignmentRole and col in (3, 5, 6):
            return Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        if role not in (Qt.ItemDataRole.EditRole, Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
            return None
        if col == 0:
            if row.slot:
                return self.hub.slot_details(row.slot)[0]
            rec = self.hub.ctl.records.get(row.device, {})
            ident = rec.get("identity") or {}
            return " / ".join(v for v in (ident.get("model"), ident.get("serial")) if v) or row.adapter
        if col == 1:
            names: dict[str, str] = self.hub.slot_details(row.slot)[1] if row.slot else {}
            return channel_label(names, row.channel)
        if col == 2:
            if row.parameters and role == Qt.ItemDataRole.ToolTipRole:
                return f"{row.label}\n{row.parameters}"
            return row.label
        if col == 3:
            return f"{reading.value:.9g}" if reading else "—"
        if col == 4:
            return reading.unit if reading else row.unit
        if col in (5, 6):
            v = row.lower if col == 5 else row.upper
            return "" if v is None else str(v)
        if col == 7:
            if not reading:
                return "—"
            try:
                stamp = datetime.fromisoformat(reading.timestamp).astimezone()
                return stamp.strftime(
                    "%Y-%m-%d %H:%M:%S" if role == Qt.ItemDataRole.ToolTipRole else "%H:%M:%S"
                )
            except ValueError:
                return reading.timestamp
        return status

    def flags(self, index):
        if not index.isValid():
            return Qt.ItemFlag.ItemIsDropEnabled
        flags = super().flags(index) | Qt.ItemFlag.ItemIsDragEnabled
        if index.column() in (5, 6):
            flags |= Qt.ItemFlag.ItemIsEditable
        return flags

    def save_rows(self, rows):
        self.hub.presets.update(self.preset, rows=tuple(rows))
        self.hub.changed.emit()

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):
        if role != Qt.ItemDataRole.EditRole or not index.isValid() or index.column() not in (5, 6):
            return False
        try:
            rows = list(self.rows)
            text = str(value).strip()
            rows[index.row()] = replace(
                rows[index.row()],
                **{"lower" if index.column() == 5 else "upper": float(text) if text else None},
            )
            self.save_rows(rows)
        except Exception as exc:
            self.hub.ctl.message.emit(f"范围设置失败 · {exc}")
            return False
        return True

    def supportedDropActions(self):
        return Qt.DropAction.MoveAction

    def mimeTypes(self):
        return [self.mime_type]

    def mimeData(self, indexes):
        mime = QMimeData()
        keys = [self.preset, *(self.rows[i].id for i in sorted({x.row() for x in indexes}))]
        mime.setData(self.mime_type, " ".join(keys).encode())
        return mime

    def dropMimeData(self, data, action, row, column, parent):
        if action != Qt.DropAction.MoveAction or not data.hasFormat(self.mime_type):
            return False
        source, *ids = bytes(data.data(self.mime_type)).decode().split()
        if source != self.preset or not ids:
            return False
        at = row if row >= 0 else parent.row() if parent.isValid() else len(self.rows)
        moved = [r for r in self.rows if r.id in ids]
        if len(moved) != len(ids):
            return False
        offset = sum(r.id not in ids for r in self.rows[:at])
        remaining = [r for r in self.rows if r.id not in ids]
        self.save_rows(remaining[:offset] + moved + remaining[offset:])
        return True


class PresetTable(QWidget):
    def __init__(self, hub, parent=None):
        super().__init__(parent)
        self.hub = hub
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        bar = QHBoxLayout()
        self.add = QPushButton("添加监视量")
        self.remove = QPushButton("移除")
        self.once = QPushButton("采一次")
        for button in (self.add, self.remove, self.once):
            bar.addWidget(button)
        bar.addStretch()
        layout.addLayout(bar)
        self.table = QTableView()
        self.model = WatchTableModel(hub, self.table)
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.table.setDragDropOverwriteMode(False)
        self.table.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.setAlternatingRowColors(True)
        header = self.table.horizontalHeader()
        for i, width in enumerate((155, 175, 140, 95, 55, 65, 65, 90, 130)):
            self.table.setColumnWidth(i, width)
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(31)
        layout.addWidget(self.table)
        self.add.clicked.connect(self.pick)
        self.remove.clicked.connect(self.remove_rows)
        self.once.clicked.connect(self.capture)
        self.table.selectionModel().selectionChanged.connect(self.buttons)
        hub.updated.connect(self.buttons)
        self.bind("")

    def bind(self, key):
        self.model.bind(key)
        self.add.setEnabled(bool(key))
        self.buttons()
        if self.isVisible():
            self.hub.subscribe(self, key)

    def buttons(self, *_args):
        rows = self.selection()
        self.remove.setEnabled(bool(rows))
        special = len(rows) == 1 and rows[0].source in ("waveform", "phase_noise_capture")
        self.once.setVisible(special)
        if special:
            device = self.hub.resolve(rows[0])
            state = self.hub.ctl.states.get(device, {})
            self.once.setEnabled(
                state.get("connectionState") == "connected"
                and not state.get("pending")
                and not state.get("monitoring")
                and not state.get("recording")
            )
            self.once.setToolTip("仅在仪器空闲时启动一次完整采集")

    def selection(self):
        return [
            self.model.rows[i.row()]
            for i in self.table.selectionModel().selectedRows()
            if i.row() < len(self.model.rows)
        ]

    def pick(self):
        picker = WatchPicker(self.hub, self)
        if picker.exec() and self.hub.presets.get(self.model.preset):
            rows: list[WatchRow] = list(self.model.rows)
            for row in picker.result_rows:
                if not any(
                    (r.slot or r.device, r.channel, r.source, r.profile, r.metric, r.parameters)
                    == (
                        row.slot or row.device,
                        row.channel,
                        row.source,
                        row.profile,
                        row.metric,
                        row.parameters,
                    )
                    for r in rows
                ):
                    rows.append(row)
            self.model.save_rows(rows)

    def remove_rows(self):
        ids = {r.id for r in self.selection()}
        self.model.save_rows(r for r in self.model.rows if r.id not in ids)

    def capture(self):
        try:
            self.hub.take_once(self.selection()[0])
        except Exception as exc:
            self.hub.ctl.message.emit(str(exc))

    def showEvent(self, event):
        super().showEvent(event)
        self.hub.subscribe(self, self.model.preset)

    def hideEvent(self, event):
        self.hub.subscribe(self, "")
        super().hideEvent(event)
