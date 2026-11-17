"""Search and select numeric signals across connected instrument channels."""

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QInputDialog,
    QLabel,
    QLineEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from lab_control.domain.workspace import channel_label


def children(parent: QTreeWidgetItem):
    for index in range(parent.childCount()):
        child = parent.child(index)
        if child is not None:
            yield child


class WatchPicker(QDialog):
    def __init__(self, hub, parent):
        super().__init__(parent)
        self.hub = hub
        self.result_rows = []
        self.entries = []
        self.setWindowTitle("添加监视量")
        self.resize(640, 560)
        layout = QVBoxLayout(self)
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索仪器、通道、测点或监视量")
        layout.addWidget(self.search)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["仪器／通道／监视量", "单位"])
        self.tree.setColumnWidth(0, 455)
        layout.addWidget(self.tree)
        for record in hub.ctl.connected_records():
            dev = record["id"]
            channels, specs = hub.catalog(dev)
            if not channels or not specs:
                continue
            ident = record.get("identity") or {}
            slot = record.get("slots", {}).get("general")
            name = hub.ctl.runtime.repository.function_slots.label(slot, "general") if slot else ""
            label = (
                " · ".join(v for v in (name, ident.get("model"), ident.get("serial")) if v)
                or record["adapterId"]
            )
            device = QTreeWidgetItem(self.tree, [label])
            names = hub.ctl.channel_names(dev)
            for ch in channels:
                channel = QTreeWidgetItem(device, [channel_label(names, ch)])
                for spec in specs.values():
                    leaf = QTreeWidgetItem(channel, [spec.label, spec.unit])
                    leaf.setCheckState(0, Qt.CheckState.Unchecked)
                    self.entries.append(
                        (
                            leaf,
                            record,
                            ch,
                            spec,
                            (label + " " + channel.text(0) + " " + spec.label + " " + spec.metric).casefold(),
                        )
                    )
        self.tree.expandToDepth(0)
        self.message = QLabel("" if self.entries else "当前没有已连接且提供监视量的仪器")
        layout.addWidget(self.message)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("添加")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.search.textChanged.connect(self.filter)

    def filter(self, text):
        words = text.casefold().split()
        for leaf, _, _, _, desc in self.entries:
            leaf.setHidden(not all(word in desc for word in words))
        for dev in children(self.tree.invisibleRootItem()):
            for ch in children(dev):
                ch.setHidden(all(leaf.isHidden() for leaf in children(ch)))
                ch.setExpanded(bool(words) and not ch.isHidden())
            dev.setHidden(all(ch.isHidden() for ch in children(dev)))
            dev.setExpanded(True)

    def accept(self):
        rows = []
        params = {}
        for leaf, record, ch, spec, _ in self.entries:
            if leaf.checkState(0) != Qt.CheckState.Checked:
                continue
            key = (record["id"], ch, spec.profile.id if spec.profile else "")
            if key not in params:
                args: dict[str, Any] = {}
                fields = spec.profile.parameters.model_fields if spec.profile else {}
                if "offset_hz" in fields or "reference_power_w" in fields:
                    offset = "offset_hz" in fields
                    label = "偏频（Hz）" if offset else "参考光功率（W）"
                    val, ok = QInputDialog.getDouble(
                        self, f"CH{ch} · {spec.label}", label, 1.0 if offset else 0.001, 1e-12, 1e12, 12
                    )
                    if not ok:
                        return
                    args = {"offset_hz": val} if offset else {"reference_power_w": {str(ch): val}}
                if spec.profile:
                    try:
                        spec.profile.parameters.model_validate(args)
                    except ValueError as exc:
                        self.message.setText(str(exc))
                        return
                params[key] = args
            rows.append(spec.row(record, ch, params[key]))
        if not rows:
            self.message.setText("请勾选至少一个监视量")
            return
        self.result_rows = rows
        super().accept()
