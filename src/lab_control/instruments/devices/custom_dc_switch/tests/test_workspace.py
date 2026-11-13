import json

import pytest
from PySide6.QtWidgets import QMessageBox, QPushButton

from lab_control.application.runtime import ApplicationRuntime
from lab_control.discovery.providers import ConfiguredDiscoveryProvider, SerialDiscoveryProvider
from lab_control.discovery.types import DiscoveryCandidate
from lab_control.domain.connections import ConnectionConfig
from lab_control.domain.instruments import DeviceError
from lab_control.instruments.devices.custom_dc_switch.adapter import ADAPTER_ID
from lab_control.instruments.devices.custom_dc_switch.hardware.readonly import verify
from lab_control.transport.visa import VisaRuntime
from lab_control.ui.real_window import RealWindow

from .test_protocol import CONFIG, Manager

PORT = DiscoveryCandidate(
    ConnectionConfig("COM99", transport="serial"), "serial", {"vid": 0x10C4, "pid": 0xEA60}
)
OTHER = DiscoveryCandidate(
    ConnectionConfig("COM2", transport="serial"), "serial", {"vid": 0x067B, "pid": 0x2303}
)


def test_manual_board_gui_only_offers_matching_bridge_and_never_sends_automatically(
    qtbot, tmp_path, monkeypatch
):
    monkeypatch.setattr("lab_control.storage.runtime.hardware_lock_home", lambda: tmp_path / "locks")
    monkeypatch.setattr("lab_control.storage.repository.application_home", lambda: tmp_path / "home")
    monkeypatch.setattr(SerialDiscoveryProvider, "enumerate", lambda _: (OTHER, PORT))
    choices = []

    def select(_parent, _title, _hint, items):
        choices.extend(items)
        return items[0], True

    monkeypatch.setattr("lab_control.ui.workspace.dialogs.choose_item", select)
    manager = Manager()
    with ApplicationRuntime(root=tmp_path, visa=VisaRuntime(manager=manager)) as runtime:
        device_id = runtime.add(CONFIG)
        providers = (ConfiguredDiscoveryProvider((CONFIG,)), SerialDiscoveryProvider())
        runtime.discovery.scan(providers).result(8)
        assert runtime.connect_all_discovered().result(8)["total"] == 0 and manager.handles == []
        with pytest.raises(DeviceError, match="USB"):
            runtime.connect_manual_serial(ADAPTER_ID, OTHER.config.resource)
        window = RealWindow(runtime)
        qtbot.addWidget(window)
        window.show()
        try:
            next(b for b in window.findChildren(QPushButton) if b.text() == "手动绑定开关板").click()
            qtbot.waitUntil(lambda: ("设备管理", "output_switch") in window.page_map)
            page = window.page_map[("设备管理", "output_switch")]
            assert choices == ["COM99"] and page.selector.device_id == device_id
            assert "RF3 和 RF4 互斥" in page.note.text()
            assert page.form.refresh_button.isHidden() and page.form.snapshot_status.isHidden()
            assert "output_switch" not in window._submenu_categories("实时监测")
            assert not page.go_monitor.isVisible() and not manager.handles
            log = page.form.action_log
            assert log.isVisible() and not log.toPlainText()
            editor = page.form.editors["ch4_on"]
            assert editor.apply_button.text() == "选择 RF4"
            assert editor.apply_button.isEnabled() and not editor.read_button.isVisible()
            monkeypatch.setattr(QMessageBox, "question", lambda *args: pytest.fail("unexpected confirmation"))
            manager.next_ack = b"rst:0x1 (POWERON_RESET)\r\nload:0x40078000\r\n"
            editor.apply_button.click()
            qtbot.waitUntil(lambda: log.toPlainText().endswith("选择 RF4   ·   已发送"))
            assert editor.status.isHidden()
            assert editor.readback.isHidden() and not editor.readback.text()
            page.form.tabs.setCurrentIndex(2)
            diag = page.form.editors["switch_board_serial_status"]
            qtbot.waitUntil(lambda: diag.readback.text() == "已发送")
            page.form.tabs.setCurrentIndex(0)
            assert manager.handles[0].writes == [b"ch4_on\n"]
            editor = page.form.editors["ch3_on"]
            qtbot.waitUntil(editor.apply_button.isEnabled)
            manager.handles[0].reply = "RF3已打开\r\n".encode()
            editor.apply_button.click()
            qtbot.waitUntil(lambda: log.toPlainText().endswith("选择 RF3   ·   RF3已打开"))
            lines = log.toPlainText().splitlines()
            assert len(lines) == 2 and lines[0].endswith("选择 RF4   ·   已发送")
            assert editor.status.isHidden()
            assert "POWERON_RESET" not in log.toPlainText()
            page.form.set_device("")
            page.form.set_device(device_id)
            assert log.toPlainText().splitlines() == lines
            assert editor.readback.isHidden() and not editor.readback.text()
            assert manager.handles[0].writes == [
                b"ch4_on\n",
                b"ch3_on\n",
            ]
        finally:
            window.timer.stop()
            window.controller.close()
            window.closing = True
            window.bridge.worker.shutdown()
        runtime.sessions.disconnect(device_id)
        reopened, result = runtime.connect_manual_serial(ADAPTER_ID, PORT.config.resource)
        assert reopened == device_id and result.result(5).key is None
        assert len(manager.handles) == 1 and manager.handles[0].closed


@pytest.mark.parametrize("available", [False, True])
def test_readonly_batch_confirms_only_port_not_outputs(tmp_path, monkeypatch, available):
    monkeypatch.setattr("lab_control.storage.runtime.hardware_lock_home", lambda: tmp_path / "locks")
    monkeypatch.setattr("lab_control.storage.repository.application_home", lambda: tmp_path / "home")
    monkeypatch.setattr(SerialDiscoveryProvider, "enumerate", lambda _: (PORT,) if available else ())
    folder = tmp_path / "hardware-tests" / "test-board" / "batch"
    report = verify(CONFIG, folder, duration=1, root=tmp_path, input_condition="offline")
    summary = json.loads((folder / "summary.json").read_text(encoding="utf-8"))
    assert report["checks_passed"] is available
    assert not summary["identity_verified"] and summary["adapter_id"] == ADAPTER_ID
    assert report["stages"]["physical_output"]["state"] == "not_tested"
    assert report["stages"]["enumeration"]["state"] == ("passed" if available else "blocked")
    assert (folder / "communication.jsonl").read_bytes() == b""
    assert report["physical_open_attempts"] == 0
    assert report["stages"]["compatibility_connection"]["state"] == "not_tested"
    assert report["stages"]["manual_rebinding"]["state"] == ("passed" if available else "not_tested")
