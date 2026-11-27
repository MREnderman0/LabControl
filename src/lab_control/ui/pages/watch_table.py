"""Editable table shared by the main watchlist and its modeless windows."""

from dataclasses import replace
from datetime import datetime

from PySide6.QtCore import QAbstractTableModel, QEvent, QMimeData, QModelIndex, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPalette
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from lab_control.application.watchlists import WatchRow
from lab_control.domain.workspace import channel_label
from lab_control.ui.kit import Pill, icons, style_button
from lab_control.ui.kit.numeric import fit_reading, number_text
from lab_control.ui.pages.watch_picker import WatchPicker
from lab_control.ui.styles import tokens as tk

STATUS_DOTS = {"正常": tk.OK, "高于上限": tk.ERR, "低于下限": tk.ERR}


class WatchReadingDelegate(QStyledItemDelegate):
    def initStyleOption(self, option, index):
        super().initStyleOption(option, index)
        if index.column() in (3, 5, 6) and option.rect.width() > 0:
            view = option.widget
            style = view.style() if view is not None else QApplication.style()
            area = style.subElementRect(QStyle.SubElement.SE_ItemViewItemText, option, view)
            margin = style.pixelMetric(QStyle.PixelMetric.PM_FocusFrameHMargin, option, view) + 1
            option.text = fit_reading(option.text, area.width() - 2 * margin, option.fontMetrics)
        color = index.data(Qt.ItemDataRole.ForegroundRole)
        if color is not None:
            option.palette.setColor(QPalette.ColorRole.HighlightedText, color)

    def paint(self, painter, option, index):
        if index.column() != WatchTableModel.columns.index("状态"):
            super().paint(painter, option, index)
            return
        # Status cell: a colored dot before the text, so alarms stand out at a glance.
        status = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        size = max(6, round(option.fontMetrics.height() * 0.42))
        shifted = QStyleOptionViewItem(option)
        shifted.rect = option.rect.adjusted(size + 8, 0, 0, 0)
        super().paint(painter, shifted, index)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(STATUS_DOTS.get(status, tk.IDLE)))
        center = option.rect.center()
        painter.drawEllipse(option.rect.left() + 8, center.y() - size // 2, size, size)
        painter.restore()


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
        if rows == self.rows:
            return
        if tuple(r.id for r in rows) != tuple(r.id for r in self.rows):
            self.beginResetModel()
            self.rows = rows
            self.endResetModel()
            return
        old, self.rows = self.rows, rows
        for i, (before, after) in enumerate(zip(old, rows, strict=True)):
            for column, changed in ((5, before.lower != after.lower), (6, before.upper != after.upper)):
                if changed:
                    cell = self.index(i, column)
                    self.dataChanged.emit(cell, cell)
        self.refresh()

    def refresh(self):
        if not self.rows:
            return
        # Qt reloads an open editor whenever its cell lies in a dataChanged range.
        for first, last in ((0, 4), (7, 8)):
            self.dataChanged.emit(
                self.index(0, first),
                self.index(len(self.rows) - 1, last),
                [Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ForegroundRole],
            )

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return len(self.columns)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.TextAlignmentRole and orientation == Qt.Orientation.Horizontal:
            side = Qt.AlignmentFlag.AlignRight if section in (3, 5, 6) else Qt.AlignmentFlag.AlignLeft
            return side | Qt.AlignmentFlag.AlignVCenter
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
                return QColor(tk.ERR)
            if status != "正常":
                return QColor(tk.TEXT_3)
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
                return f"{row.label}\n{row.parameters}\n双击查看记录曲线"
            return "双击查看记录曲线" if role == Qt.ItemDataRole.ToolTipRole else row.label
        if col == 3:
            value = number_text(reading.value) if reading else "—"
            if role == Qt.ItemDataRole.ToolTipRole:
                return f"{value} {reading.unit}\n双击查看记录曲线" if reading else "双击查看记录曲线"
            return value
        if col == 4:
            return reading.unit if reading else row.unit
        if col in (5, 6):
            v = row.lower if col == 5 else row.upper
            if role == Qt.ItemDataRole.ToolTipRole:
                value = "" if v is None else f"{number_text(v)} {row.unit}\n"
                return f"{value}单击设置{self.columns[col]}，留空可清除"
            if v is None:
                return "" if role == Qt.ItemDataRole.EditRole else "—"
            return str(v)
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
    open_window = Signal()

    def __init__(self, hub, parent=None, *, detached=False):
        super().__init__(parent)
        self.hub = hub
        self.detached = detached
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.add = QPushButton("添加监视量")
        self.remove = QPushButton("移除")
        self.once = QPushButton("采一次")
        if not detached:
            self.add.setObjectName("primaryButton")
            self.add.setIcon(icons.icon("plus", tk.ACCENT_INK, disabled=tk.TEXT_3))
            for button in (self.add, self.remove, self.once):
                button.setProperty("buttonSize", "compact")
        self.record = QPushButton("开始记录")
        self.record.setProperty("buttonSize", "compact")
        self.record.clicked.connect(self.toggle_recording)
        self.record_status = Pill(dot=True)
        for button in (self.add, self.remove, self.once):
            bar.addWidget(button)
        bar.addStretch()
        self.summary = QLabel()
        self.summary.setObjectName("watchSummary")
        bar.addWidget(self.summary)
        if not detached:
            self.window_button = QPushButton("独立窗口")
            self.window_button.setProperty("buttonSize", "compact")
            self.window_button.setIcon(icons.icon("expand", tk.TEXT_2))
            self.window_button.setToolTip("在独立窗口中打开当前预设（大字号，可放到副屏）")
            self.window_button.clicked.connect(self.open_window)
            bar.addWidget(self.window_button)
        layout.addLayout(bar)
        record_bar = QHBoxLayout()
        record_bar.setSpacing(8)
        record_bar.addWidget(self.record)
        record_bar.addWidget(self.record_status)
        record_bar.addStretch()
        layout.addLayout(record_bar)
        self.table = QTableView()
        self.table.setObjectName("watchReadings")
        self.model = WatchTableModel(hub, self.table)
        self.table.setModel(self.model)
        self.table.setItemDelegate(WatchReadingDelegate(self.table))
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.table.setDragDropOverwriteMode(False)
        self.table.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.table.setAlternatingRowColors(True)
        header = self.table.horizontalHeader()
        widths = (
            (192, 362, 146, 228, 106, 132, 132, 162, 156)
            if detached
            else (140, 226, 130, 102, 52, 70, 70, 88, 115)
        )
        self.column_weights: tuple[int, ...] = widths
        for i, width in enumerate(widths):
            self.table.setColumnWidth(i, width)
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)
        header.sectionResized.connect(self.remember_column_widths)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(64 if detached else 34)
        layout.addWidget(self.table)
        self.empty = QLabel(
            "这个预设还没有监视量\n点击“添加监视量”，选择设备通道和要盯的读数", self.table.viewport()
        )
        self.empty.setObjectName("watchEmpty")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.hide()
        self.table.viewport().installEventFilter(self)
        self.add.clicked.connect(self.pick)
        self.remove.clicked.connect(self.remove_rows)
        self.once.clicked.connect(self.capture)
        self.table.selectionModel().selectionChanged.connect(self.buttons)
        self.table.clicked.connect(self.edit_limit)
        self.table.doubleClicked.connect(self.show_trend)
        hub.changed.connect(self.buttons)
        hub.updated.connect(self.buttons)
        hub.updated.connect(self.update_summary)
        self.model.modelReset.connect(self.update_summary)
        self.model.rowsInserted.connect(self.update_summary)
        self.model.rowsRemoved.connect(self.update_summary)
        self.bind("")

    def remember_column_widths(self, *_args):
        header = self.table.horizontalHeader()
        if header.underMouse() and QApplication.mouseButtons() == Qt.MouseButton.LeftButton:
            self.column_weights = tuple(header.sectionSize(i) for i in range(header.count()))

    def fit_columns(self):
        # Reserve readable headers, then divide the remaining room by the user's column proportions.
        header = self.table.horizontalHeader()
        metrics = header.fontMetrics()
        minimums = [
            max(header.minimumSectionSize(), metrics.horizontalAdvance(title) + 14)
            for title in self.model.columns
        ]
        cells = self.table.fontMetrics()
        # These cells carry a sign/exponent, a clock, or an extra status dot.
        samples = ((3, "-1e-123", 22), (7, "23:59:59", 22), (8, "高于上限", 38))
        wanted = {
            i: max(0, cells.horizontalAdvance(sample) + pad - minimums[i]) for i, sample, pad in samples
        }
        available = self.table.viewport().width()
        fraction = min(1.0, max(0, available - sum(minimums)) / max(1, sum(wanted.values())))
        for column, pixels in wanted.items():
            minimums[column] += int(pixels * fraction)
        extra = max(0, available - sum(minimums))
        total = sum(self.column_weights)
        for i, (minimum, weight) in enumerate(zip(minimums[:-1], self.column_weights)):
            header.resizeSection(i, minimum + extra * weight // total)
        header.resizeSection(len(minimums) - 1, minimums[-1])

    def eventFilter(self, watched, event):
        if watched is self.table.viewport() and event.type() == QEvent.Type.Resize:
            if event.size().width() != event.oldSize().width():
                self.fit_columns()
            self.empty.setGeometry(self.table.viewport().rect())
        return super().eventFilter(watched, event)

    def update_summary(self, *_args):
        rows = self.model.rows
        statuses = [self.hub.status.get(r.id, "等待读数") for r in rows]
        alarms = sum(s in {"高于上限", "低于下限"} for s in statuses)
        waiting = sum(s not in {"正常", "高于上限", "低于下限"} for s in statuses)
        parts = [f"共 {len(rows)} 项"]
        if alarms:
            parts.append(f'<span style="color:{tk.ERR}">超限 {alarms}</span>')
        if waiting:
            parts.append(f"未就绪 {waiting}")
        self.summary.setText(" · ".join(parts) if self.model.preset else "")
        self.empty.setGeometry(self.table.viewport().rect())
        self.empty.setVisible(bool(self.model.preset) and not rows)
        if not self.detached:
            self.window_button.setEnabled(bool(self.model.preset))

    def bind(self, key):
        self.model.bind(key)
        self.add.setEnabled(bool(key))
        self.buttons()
        self.update_summary()
        if self.isVisible():
            self.hub.subscribe(self, key)

    def edit_limit(self, index):
        if (
            index.column() in (5, 6)
            and QApplication.keyboardModifiers() == Qt.KeyboardModifier.NoModifier
            and self.table.state() != QAbstractItemView.State.EditingState
        ):
            self.table.edit(index)

    def show_trend(self, index):
        if index.isValid() and index.column() in (2, 3):
            self.hub.open_trend(self.model.preset, self.model.rows[index.row()])

    def toggle_recording(self):
        key = self.model.preset
        run = self.hub.recordings.get(key)
        try:
            if run and run.accepting:
                self.hub.stop_recording(key)
            else:
                self.hub.start_recording(key)
        except Exception as exc:
            self.hub.ctl.message.emit(f"记录操作失败 · {exc}")
        self.buttons()

    def buttons(self, *_args):
        run = self.hub.recordings.get(self.model.preset)
        saving = run and not run.accepting and not run.done.is_set()
        self.record.setText("停止记录" if run and run.accepting else "正在保存…" if saving else "开始记录")
        self.record.setEnabled(bool(self.model.rows or (run and run.accepting)) and not saving)
        active = bool(run and run.accepting)
        style_button(self.record, "danger" if active else "", "stop" if active else "record", "sm", tk.ERR)
        self.record_status.setVisible(bool(run))
        if run:
            state = (
                "写入失败" if run.error else "记录中" if run.accepting else "保存中" if saving else "已保存"
            )
            self.record_status.set_kind("err" if run.error else "warn" if saving else "ok")
            self.record_status.set_text(f"{state} · {run.count} 点")
            self.record_status.setToolTip(
                "\n".join(filter(None, (run.error, run.last_stamp, str(run.path.parent))))
            )
        else:
            self.record_status.set_text("")
            self.record_status.setToolTip("")

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
