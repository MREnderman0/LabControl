"""Named watch presets with shared, modeless live table windows."""

from dataclasses import replace

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFrame,
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
from lab_control.ui.kit import icons
from lab_control.ui.pages.watch_runtime import WatchRuntime
from lab_control.ui.pages.watch_table import PresetTable
from lab_control.ui.styles import tokens as tk
from lab_control.ui.widgets.popup_surface import WorkspaceMenu


class WatchlistsPage(QWidget):
    def __init__(self, hub: WatchRuntime, parent=None):
        super().__init__(parent)
        self.hub = hub
        self.windows: dict[str, QDialog] = {}
        layout = QHBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        split = QSplitter(Qt.Orientation.Horizontal)
        split.setObjectName("watchSplit")
        split.setHandleWidth(14)
        layout.addWidget(split)
        pane = QFrame()
        pane.setObjectName("watchPresetPane")
        left = QVBoxLayout(pane)
        left.setContentsMargins(10, 10, 10, 10)
        left.setSpacing(8)
        bar = QHBoxLayout()
        bar.setContentsMargins(4, 0, 0, 0)
        title = QLabel("预设")
        title.setObjectName("panelTitle")
        bar.addWidget(title)
        bar.addStretch()
        self.create_button = QPushButton("新建")
        self.create_button.setProperty("buttonSize", "compact")
        self.create_button.setIcon(icons.icon("plus", tk.TEXT_2))
        self.create_button.setToolTip("新建监视预设；双击预设打开独立窗口，右键可重命名、复制或删除")
        bar.addWidget(self.create_button)
        left.addLayout(bar)
        self.list = QListWidget()
        self.list.setObjectName("watchPresets")
        self.list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.list.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        left.addWidget(self.list, 1)
        self.presets_empty = QLabel("还没有监视预设\n点击“新建”，把需要盯着的通道放进一个预设")
        self.presets_empty.setObjectName("watchEmpty")
        self.presets_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.presets_empty.setWordWrap(True)
        left.addWidget(self.presets_empty, 1)
        self.pin_button = QPushButton("置顶到首页")
        self.pin_button.setEnabled(False)
        self.pin_button.setProperty("buttonSize", "compact")
        self.pin_button.setIcon(icons.icon("pin", tk.TEXT_2))
        self.pin_button.clicked.connect(self.toggle_pin)
        left.addWidget(self.pin_button)
        hint = QLabel("双击打开独立窗口 · 拖动调整顺序")
        hint.setObjectName("watchHint")
        left.addWidget(hint)
        self.table = PresetTable(self.hub)
        self.table.open_window.connect(lambda: self.open_window(self.key(self.list.currentItem())))
        split.addWidget(pane)
        split.addWidget(self.table)
        pane.setMinimumWidth(170)
        split.setStretchFactor(1, 1)
        split.setSizes([230, 900])
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
        items = sorted(self.hub.presets.items, key=lambda p: not p.pinned)
        expected = [(p.id, p.name, p.pinned) for p in items]
        shown = [self.list.item(i) for i in range(self.list.count())]
        if expected != [(self.key(item), item.text(), not item.icon().isNull()) for item in shown]:
            self.list.blockSignals(True)
            self.list.clear()
            for key, name, pinned in expected:
                item = QListWidgetItem(name, self.list)
                item.setData(Qt.ItemDataRole.UserRole, key)
                if pinned:
                    item.setIcon(icons.icon("pin", tk.ACCENT))
                    item.setToolTip("已置顶到首页")
                    item.setData(Qt.ItemDataRole.AccessibleDescriptionRole, "已置顶到首页")
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsDragEnabled)
            self.list.setCurrentRow(
                next((i for i, p in enumerate(items) if p.id == current), 0) if items else -1
            )
            self.list.blockSignals(False)
            self.select(self.list.currentItem())
        self.list.setVisible(bool(items))
        self.presets_empty.setVisible(not items)
        for key, dialog in list(self.windows.items()):
            preset = self.hub.presets.get(key)
            if preset:
                dialog.setWindowTitle(preset.name + " · 监视列表")
            else:
                dialog.close()

    def select(self, item, _old=None):
        key = self.key(item)
        preset = self.hub.presets.get(key)
        self.pin_button.setEnabled(preset is not None)
        self.pin_button.setText("取消置顶" if preset and preset.pinned else "置顶到首页")
        self.table.bind(key)

    def toggle_pin(self):
        preset = self.hub.presets.get(self.key(self.list.currentItem()))
        if preset:
            self.hub.presets.pin("" if preset.pinned else preset.id)
            self.hub.changed.emit()

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
        presets = self.hub.presets
        unpinned = iter(p for key in order if (p := presets.get(key)) and not p.pinned)
        self.hub.save(p if p.pinned else next(unpinned) for p in presets.items)

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
