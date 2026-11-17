"""Named watch presets with shared, modeless live table windows."""

from dataclasses import replace

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from lab_control.application.watchlists import Preset, new_id
from lab_control.ui.pages.watch_runtime import WatchRuntime
from lab_control.ui.pages.watch_table import PresetTable
from lab_control.ui.widgets.popup_surface import WorkspaceMenu


class WatchlistsPage(QWidget):
    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.hub = WatchRuntime(controller, self)
        self.windows = {}
        layout = QHBoxLayout(self)
        split = QSplitter(Qt.Orientation.Horizontal)
        split.setObjectName("watchSplit")
        split.setHandleWidth(8)
        layout.addWidget(split)
        pane = QWidget()
        left = QVBoxLayout(pane)
        left.setContentsMargins(0, 0, 0, 0)
        bar = QHBoxLayout()
        title = QLabel("预设")
        title.setObjectName("panelTitle")
        bar.addWidget(title)
        bar.addStretch()
        self.create_button = QPushButton("新建")
        bar.addWidget(self.create_button)
        left.addLayout(bar)
        self.list = QListWidget()
        self.list.setObjectName("watchPresets")
        self.list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.list.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        left.addWidget(self.list)
        self.table = PresetTable(self.hub)
        split.addWidget(pane)
        split.addWidget(self.table)
        pane.setMinimumWidth(145)
        split.setStretchFactor(1, 1)
        split.setSizes([200, 900])
        self.create_button.clicked.connect(self.create_preset)
        self.list.currentItemChanged.connect(self.select)
        self.list.itemDoubleClicked.connect(lambda item: self.open_window(self.key(item)))
        self.list.customContextMenuRequested.connect(self.menu)
        self.list.model().rowsMoved.connect(self.reordered)
        self.hub.changed.connect(self.reload)
        self.reload()

    @staticmethod
    def key(item):
        return item.data(Qt.ItemDataRole.UserRole) if item else ""

    def reload(self, selected=""):
        current = selected or self.key(self.list.currentItem())
        items = self.hub.presets.items
        expected = [(p.id, p.name) for p in items]
        if expected != [
            (self.key(self.list.item(i)), self.list.item(i).text()) for i in range(self.list.count())
        ]:
            self.list.blockSignals(True)
            self.list.clear()
            for key, name in expected:
                item = QListWidgetItem(name, self.list)
                item.setData(Qt.ItemDataRole.UserRole, key)
            self.list.setCurrentRow(
                next((i for i, p in enumerate(items) if p.id == current), 0) if items else -1
            )
            self.list.blockSignals(False)
            self.select(self.list.currentItem())
        for key, dialog in list(self.windows.items()):
            preset = self.hub.presets.get(key)
            if preset:
                dialog.setWindowTitle(preset.name + " · 监视列表")
            else:
                dialog.close()

    def select(self, item, _old=None):
        self.table.bind(self.key(item))

    def select_id(self, key):
        for i in range(self.list.count()):
            if self.key(self.list.item(i)) == key:
                self.list.setCurrentRow(i)
                break

    def create_preset(self):
        name, ok = QInputDialog.getText(self, "新建预设", "预设名称")
        if ok and name.strip():
            p = Preset(name.strip())
            self.hub.save((*self.hub.presets.items, p))
            self.select_id(p.id)

    def reordered(self, *_):
        order = [self.key(self.list.item(i)) for i in range(self.list.count())]
        self.hub.save(self.hub.presets.get(k) for k in order)

    def menu(self, pos):
        item = self.list.itemAt(pos)
        if item is None:
            return
        self.list.setCurrentItem(item)
        key = self.key(item)
        p = self.hub.presets.get(key)
        menu = WorkspaceMenu(self)
        opened = menu.addAction("打开独立窗口")
        rename = menu.addAction("重命名")
        duplicate = menu.addAction("复制预设")
        menu.addSeparator()
        delete = menu.addAction("删除预设")
        action = menu.exec(self.list.viewport().mapToGlobal(pos))
        if action is opened:
            self.open_window(key)
        elif action is rename:
            name, ok = QInputDialog.getText(self, "重命名预设", "预设名称", text=p.name)
            if ok and name.strip():
                self.hub.presets.update(key, name=name.strip())
                self.hub.changed.emit()
        elif action is duplicate:
            clone = Preset(p.name + " 副本", tuple(replace(r, id=new_id()) for r in p.rows))
            self.hub.save((*self.hub.presets.items, clone))
            self.select_id(clone.id)
        elif (
            action is delete
            and QMessageBox.question(self, "删除预设", f"删除「{p.name}」？")
            == QMessageBox.StandardButton.Yes
        ):
            self.hub.save(v for v in self.hub.presets.items if v.id != key)

    def open_window(self, key):
        if key in self.windows:
            dialog = self.windows[key]
            dialog.showNormal()
            dialog.raise_()
            dialog.activateWindow()
            return dialog
        p = self.hub.presets.get(key)
        if not p:
            return None
        dialog = QDialog(self.window(), Qt.WindowType.Window)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.setWindowTitle(p.name + " · 监视列表")
        dialog.setObjectName("watchWindow")
        table = PresetTable(self.hub, dialog, detached=True)
        table.bind(key)
        QVBoxLayout(dialog).addWidget(table)
        available = self.screen().availableGeometry()
        width = table.table.horizontalHeader().length() + 48
        dialog.resize(min(width, available.width() - 32), min(680, available.height() - 64))
        self.windows[key] = dialog
        dialog.finished.connect(lambda _: self.windows.pop(key, None))
        dialog.show()
        return dialog

    def shutdown(self):
        for dialog in list(self.windows.values()):
            dialog.close()
        self.hub.close()
