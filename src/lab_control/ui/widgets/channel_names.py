from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from lab_control.application.channel_names import current_binding, save_names
from lab_control.domain.instruments import ValidationError


class ChannelNamesDialog(QDialog):
    def __init__(self, runtime, device_id: str, mode: str, parent=None):
        super().__init__(parent)
        self.runtime = runtime
        self.binding = current_binding(runtime, device_id, mode)
        slots = runtime.repository.function_slots
        self.setWindowTitle("编辑通道名 · " + slots.label(self.binding.slot, mode))
        self.resize(420, 480)
        layout = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        form = QFormLayout(body)
        names = slots.channel_names(self.binding.slot, mode)
        self.editors = {}
        for ch in self.binding.channels:
            field = QLineEdit(names.get(str(ch), ""))
            field.setMaxLength(64)
            field.setPlaceholderText(f"CH {ch}")
            form.addRow(f"CH {ch}", field)
            self.editors[ch] = field
        scroll.setWidget(body)
        layout.addWidget(scroll)
        self.message = QLabel()
        self.message.setWordWrap(True)
        self.message.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.message)
        reset = QPushButton("重置全部通道名")
        reset.setToolTip("立即清除此槽位全部通道名，包括当前未显示的通道")
        reset.clicked.connect(self.reset_names)
        layout.addWidget(reset)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("关闭")
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def reset_names(self):
        try:
            save_names(self.runtime, self.binding, None)
        except ValidationError as exc:
            self.message.setText(str(exc))
            return
        for field in self.editors.values():
            field.clear()
            field.setModified(False)
        self.message.setText("全部通道名已重置")

    def save(self):
        edits = {ch: field.text() for ch, field in self.editors.items() if field.isModified()}
        try:
            save_names(self.runtime, self.binding, edits)
        except ValidationError as exc:
            self.message.setText(str(exc))
            return
        self.accept()
