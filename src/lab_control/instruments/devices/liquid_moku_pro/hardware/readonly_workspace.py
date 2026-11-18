"""Exercise the production category cards and panels without arming an instrument."""

from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication

from lab_control.app import load_stylesheet, load_ui_font
from lab_control.domain.instruments import DeviceError
from lab_control.ui.real_window import RealWindow

from ..adapter import LABELS
from ..workspace import RolePage


def inspect_workspace(runtime, device_id, folder):
    app = QApplication.instance() or QApplication([])
    app.setStyle("Fusion")
    app.setStyleSheet(load_stylesheet())
    app.setFont(QFont(load_ui_font(), 10))
    window = RealWindow(runtime)
    roles = []
    try:
        window.show()
        for role, label in LABELS.items():
            window.navigate("首页")
            app.processEvents()
            window.cards[role].clicked.emit(role)
            window.open_details(device_id)
            button = window.action_buttons["manage" if role == "signal_generator" else "monitor"]
            if not button.isEnabled() or not button.isVisible() or window.rows[device_id].isHidden():
                raise DeviceError(f"{label}分类的设备入口不可用")
            button.click()
            app.processEvents()
            panel = window.stack.currentWidget()
            if not isinstance(panel, RolePage) or (panel.device_id, panel.role) != (device_id, role):
                raise DeviceError(f"{label}分类未打开指定资产的面板")
            if panel.channel != 1 or list(
                panel.channels.itemText(i) for i in range(panel.channels.count())
            ) != ["ch1", "ch2", "ch3", "ch4"]:
                raise DeviceError(f"{label}通道显示不符")
            state = window.controller.states[device_id]
            if (
                state["connectionState"] != "connected"
                or state["lastError"]
                or state["capabilityDetails"].get("workspace_attached")
            ):
                raise DeviceError(f"{label}只读打开后会话状态不符")
            image = f"ui-{role}.png"
            if not window.grab().save(str(folder / image)):
                raise DeviceError(f"无法保存{label}界面证据")
            roles.append(
                {"role": label, "channel": "ch1", "session": state["sessionId"], "screenshot": image}
            )
        if len({r["session"] for r in roles}) != 1:
            raise DeviceError("三个面板未复用同一会话")
        return roles
    finally:
        window.timer.stop()
        window.controller.close()
        window.agent_panel.timer.stop()
        window.agent_panel.shutdown()
        window.environment_panel.shutdown()
        window.storage_panel.shutdown()
        window.closing = True
        window.close()
        window.bridge.worker.shutdown(wait=True)
