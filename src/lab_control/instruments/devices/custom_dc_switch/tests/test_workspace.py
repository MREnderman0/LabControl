import json
from dataclasses import replace

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


def test_bulk_and_manual_board_gui_verify_identity_without_switching_outputs(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr("lab_control.storage.runtime.hardware_lock_home", lambda: tmp_path / "locks")
    monkeypatch.setattr("lab_control.storage.repository.application_home", lambda: tmp_path / "home")
    monkeypatch.setattr(SerialDiscoveryProvider, "enumerate", lambda _: (OTHER, PORT))
    choices = []

    def select(_parent, _title, _hint, items):
        choices.extend(items)
        return items[0], True

    monkeypatch.setattr("lab_control.ui.workspace.dialogs.choose_item", select)
    manager = Manager(monkeypatch)
    with ApplicationRuntime(root=tmp_path, visa=VisaRuntime(manager=manager)) as runtime:
        device_id = runtime.add(CONFIG)
        providers = (ConfiguredDiscoveryProvider((CONFIG,)), SerialDiscoveryProvider())
        runtime.discovery.scan(providers).result(8)
        assert not manager.handles  # Enumeration only, even with a configured alias.
        batch = runtime.connect_all_discovered().result(8)
        assert [(i["device_id"], i["status"]) for i in batch["items"]] == [(device_id, "connected")]
        assert manager.handles[0].writes == [b"*IDN?\n"]
        assert runtime.connect_all_discovered().result(8)["items"][0]["status"] == "already_connected"
        runtime.sessions.disconnect(device_id)
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
            assert not page.go_monitor.isVisible() and manager.handles[-1].writes == [b"*IDN?\n"]
            log = page.form.action_log
            assert log.isVisible() and not log.toPlainText()
            editor = page.form.editors["ch4_on"]
            assert editor.apply_button.text() == "选择 RF4"
            assert editor.apply_button.isEnabled() and not editor.read_button.isVisible()
            monkeypatch.setattr(QMessageBox, "question", lambda *args: pytest.fail("unexpected confirmation"))
            manager.handles[-1].reply = b"rst:0x1 (POWERON_RESET)\r\nload:0x40078000\r\n"
            editor.apply_button.click()
            qtbot.waitUntil(lambda: log.toPlainText().endswith("选择 RF4   ·   已发送"))
            assert editor.status.isHidden()
            assert editor.readback.isHidden() and not editor.readback.text()
            page.form.tabs.setCurrentIndex(2)
            diag = page.form.editors["switch_board_serial_status"]
            qtbot.waitUntil(lambda: diag.readback.text() == "已发送")
            page.form.tabs.setCurrentIndex(0)
            assert manager.handles[-1].writes == [b"*IDN?\n", b"ch4_on\n"]
            editor = page.form.editors["ch3_on"]
            qtbot.waitUntil(editor.apply_button.isEnabled)
            manager.handles[-1].reply = b"\xff" * 64 + "RF3已打开\r\n".encode()
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
            assert manager.handles[-1].writes == [
                b"*IDN?\n",
                b"ch4_on\n",
                b"ch3_on\n",
            ]
            runtime.sessions.disconnect(device_id)
            reopened, result = runtime.connect_manual_serial(ADAPTER_ID, PORT.config.resource)
            assert reopened == device_id and result.result(5).key is None
            assert len(manager.handles) == 3 and all(not h.is_open for h in manager.handles[:-1])
            assert manager.handles[-1].writes == [b"*IDN?\n"]
        finally:
            window.close()
            qtbot.waitUntil(lambda: window.closing and not window.isVisible(), timeout=8000)
        assert runtime.closed

    for handle in manager.handles:
        assert handle.events == [("open", "COM99", False, False), ("close", "COM99", False, False)]
    assert [handle.writes for handle in manager.handles] == [
        [b"*IDN?\n"],
        [b"*IDN?\n", b"ch4_on\n", b"ch3_on\n"],
        [b"*IDN?\n"],
    ]


@pytest.mark.parametrize("available", [False, True])
def test_readonly_batch_confirms_model_without_outputs(tmp_path, monkeypatch, available):
    monkeypatch.setattr("lab_control.storage.runtime.hardware_lock_home", lambda: tmp_path / "locks")
    monkeypatch.setattr("lab_control.storage.repository.application_home", lambda: tmp_path / "home")
    monkeypatch.setattr(SerialDiscoveryProvider, "enumerate", lambda _: (PORT,) if available else ())
    folder = tmp_path / "hardware-tests" / "test-board" / "batch"
    manager = Manager(monkeypatch)
    monkeypatch.setattr(
        "lab_control.instruments.devices.custom_dc_switch.hardware.readonly.VisaRuntime",
        lambda: VisaRuntime(manager=manager),
    )
    report = verify(CONFIG, folder, duration=1, root=tmp_path, input_condition="offline")
    summary = json.loads((folder / "summary.json").read_text(encoding="utf-8"))
    assert report["checks_passed"] is available
    assert not summary["identity_verified"] and summary["adapter_id"] == ADAPTER_ID
    assert report["stages"]["physical_output"]["state"] == "not_tested"
    assert report["stages"]["enumeration"]["state"] == ("passed" if available else "blocked")
    messages = [
        json.loads(line) for line in (folder / "communication.jsonl").read_text(encoding="utf8").splitlines()
    ]
    assert [m["hex"] for m in messages if m["direction"] == "tx"] == [b"*IDN?\n".hex(" ")] * (
        2 if available else 0
    )
    assert all(h.writes == [b"*IDN?\n"] for h in manager.handles)
    assert report["stages"]["compatibility_connection"]["state"] == ("passed" if available else "not_tested")
    assert report["stages"]["manual_rebinding"]["state"] == ("passed" if available else "not_tested")


@pytest.mark.parametrize(
    "idn,expected", [(b"ESP32-S32 Board!\r\n", "connected"), (b"other board\r\n", "failed")]
)
def test_new_unbound_bridge_model_is_verified_on_bulk_connect(tmp_path, monkeypatch, idn, expected):
    monkeypatch.setattr("lab_control.storage.runtime.hardware_lock_home", lambda: tmp_path / "locks")
    monkeypatch.setattr("lab_control.storage.repository.application_home", lambda: tmp_path / "home")
    bridge = replace(PORT, config=replace(PORT.config, usb_instance_id="test-bridge"))
    monkeypatch.setattr(SerialDiscoveryProvider, "enumerate", lambda _: (OTHER, bridge))
    manager = Manager(monkeypatch)
    manager.next_idn = idn
    with ApplicationRuntime(root=tmp_path, visa=VisaRuntime(manager=manager)) as runtime:
        runtime.discovery.scan((SerialDiscoveryProvider(),)).result(8)
        assert not manager.handles
        batch = runtime.connect_all_discovered().result(10)
        assert len(batch["items"]) == 1
        item = batch["items"][0]
        assert item["status"] == expected, item
        assert manager.handles[0].writes == [b"*IDN?\n"]
        record = next(r for r in runtime.inventory.all() if r["id"] == item["device_id"])
        if expected == "connected":
            assert record["identity"]["raw"] == "ESP32-S32 Board!"
            assert record["config"]["usb_instance_id"] == "test-bridge"
            runtime.sessions.disconnect(item["device_id"])
            bridge = replace(bridge, config=replace(bridge.config, resource="COM100"))
            runtime.discovery.scan((SerialDiscoveryProvider(),)).result(8)
            record = next(r for r in runtime.inventory.all() if r["id"] == item["device_id"])
            assert record["config"]["resource"] == "COM100" and len(manager.handles) == 1
        else:
            assert item["error_code"] == "identity_mismatch" and not manager.handles[0].is_open
            assert not record["identity"] and not runtime.leases.owner(PORT.config.resource)
    assert manager.handles[0].events == [("open", "COM99", False, False), ("close", "COM99", False, False)]
