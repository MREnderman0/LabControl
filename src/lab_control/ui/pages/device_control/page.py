from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from lab_control.application.controls import CATEGORY_LABELS
from lab_control.instruments.registry import DEVICE_DEFINITIONS
from lab_control.ui.controller import WorkspaceController
from lab_control.ui.kit import EmptyState, icons
from lab_control.ui.pages.device_control.form import ControlForm
from lab_control.ui.widgets.device_selector import DeviceSelector


class DeviceControlPage(QWidget):
    def __init__(self, controller: WorkspaceController, category: str, parent=None) -> None:
        super().__init__(parent)
        self.controller, self.category = controller, category
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(14)
        heading = QLabel(CATEGORY_LABELS[category])
        heading.setObjectName("pageTitle")
        heading.hide()  # Header breadcrumb names the page.
        layout.addWidget(heading)
        self.selector = DeviceSelector(controller, category, self, compact=True, surface="management")
        layout.addWidget(self.selector)
        self.note = QLabel()
        self.note.setObjectName("mutedText")
        self.note.setWordWrap(True)
        layout.addWidget(self.note)
        self.empty = EmptyState(
            f"未选择{CATEGORY_LABELS[category]}",
            f"请先在首页连接{CATEGORY_LABELS[category]}，再从上方选择设备。",
            icons.CATEGORY_ICONS.get(category, "sliders"),
            self,
        )
        layout.addWidget(self.empty, 1)
        self.form = ControlForm(controller)
        layout.addWidget(self.form, 1)
        self.selector.selected.connect(self.select_device)
        self.selector.channel_changed.connect(lambda channel: self.select_device(self.selector.device_id))
        controller.refreshed.connect(self.refresh)
        self.select_device(self.selector.device_id)

    def select_device(self, device_id: str) -> None:
        self.form.setVisible(bool(device_id))
        self.empty.setVisible(not device_id)
        self.note.setVisible(bool(device_id))
        self.form.set_device(device_id, self.selector.channel.currentData() or 1)
        if device_id:
            caps = self.controller.capabilities(device_id)
            text = f"已注册 {len(caps.operations)} 项能力 · {len(caps.channels)} 个通道。"
            adapter = self.controller.records[device_id]["adapterId"]
            definition = DEVICE_DEFINITIONS.get(adapter)
            if self.controller.runtime.hardware and definition and definition.manual_connection_note:
                text = definition.manual_connection_note
            elif adapter.startswith("sim."):
                text += " 此类别仅为演示，真实型号和协议待补。"
            self.note.setText(text)
        else:
            self.note.setText("暂无选中的已连接设备。请先在设备总览连接设备。")

    def refresh(self) -> None:
        if self.isVisible() and hasattr(self.form, "mode"):
            if self.form.mode != self.controller.mode:
                self.select_device(self.selector.device_id)
