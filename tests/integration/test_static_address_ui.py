import pytest
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication, QDialogButtonBox, QLineEdit, QPushButton

from lab_control.application.runtime import ApplicationRuntime
from lab_control.ui.real_window import RealWindow
from lab_control.ui.widgets.workspace_combo import WorkspaceComboBox


@pytest.mark.parametrize("adapter,port", [("ceyear.6362d", 10001), ("siglent.sps5000x", 5025)])
def test_address_toolbar_opens_shared_dialog_and_persists_entry(qtbot, tmp_path, adapter, port):
    runtime = ApplicationRuntime(root=tmp_path, hardware=False)
    window = RealWindow(runtime)
    qtbot.addWidget(window)
    window.timer.stop()
    window.controller.timer.stop()
    seen = []

    def interact():
        dialog = QApplication.activeModalWidget()
        assert dialog is not None
        model = dialog.findChild(WorkspaceComboBox)
        assert model.currentData() == ""
        from PySide6.QtWidgets import QLabel, QSpinBox

        port_field = dialog.findChild(QSpinBox, "staticSocketPort")
        search = dialog.findChild(QLineEdit, "staticModelSearch")
        search.setText("dho924")
        assert model.count() == 2
        model.setCurrentIndex(model.findData("rigol.dho924"))
        assert port_field.value() == 5555
        model.setCurrentIndex(0)
        assert port_field.value() == 0
        search.clear()
        index = model.findData(adapter)
        assert index >= 0
        model.setCurrentIndex(index)
        address = dialog.findChild(QLineEdit, "staticAddressInput")
        address.setText("192.168.0.120")
        save = next(button for button in dialog.findChildren(QPushButton) if button.text() == "保存并扫描")
        qtbot.mouseClick(save, Qt.MouseButton.LeftButton)
        (entry,) = runtime.static_addresses.entries()
        assert (entry.address, entry.adapter, entry.port) == ("192.168.0.120", adapter, port)
        address.setText("192.168.0.120:" + str(port))
        preview = dialog.findChild(QLabel, "staticEndpointPreview")
        assert f"SCPI Socket 192.168.0.120:{port}" in preview.text()
        qtbot.mouseClick(save, Qt.MouseButton.LeftButton)
        assert runtime.static_addresses.entries() == (entry,)
        edit = next(b for b in dialog.findChildren(QPushButton) if b.text() == "修改选中")
        qtbot.mouseClick(edit, Qt.MouseButton.LeftButton)
        address.setText("192.168.0.121:6123")
        qtbot.mouseClick(save, Qt.MouseButton.LeftButton)
        (fixed,) = runtime.static_addresses.entries()
        assert (fixed.id, fixed.address, fixed.port) == (entry.id, "192.168.0.121", 6123)
        seen.append("saved")
        close = dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Close)
        qtbot.mouseClick(close, Qt.MouseButton.LeftButton)

    try:
        assert window.address_list_button.text() == "地址列表"
        assert all(button.text() != "添加资源" for button in window.findChildren(QPushButton))
        QTimer.singleShot(0, interact)
        window.show_static_addresses()
        assert seen == ["saved"]
    finally:
        window.closing = True
        window.controller.close()
        window.bridge.worker.shutdown()
        runtime.close()
