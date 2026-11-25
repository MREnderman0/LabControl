import pytest

from lab_control.application.config import ConnectionConfig
from lab_control.application.runtime import ApplicationRuntime
from lab_control.instruments.registry import build_scpi_adapter
from lab_control.transport.base import MockTransport
from lab_control.ui.pages.device_control.electronic_load import ElectronicLoadPage
from lab_control.ui.real_window import RealWindow


@pytest.mark.parametrize("selection,channels", [("ALL", tuple(range(1, 9))), ("6", (6,))])
def test_load_management_readback_and_export(
    qtbot, tmp_path, monkeypatch, selection, channels, frame_export_directory
):
    commands = []
    replies = {
        "*IDN?": "DUOHEYI,DH-8318,TEST0001,VER:TEST",
        "CH?": selection,
        "MEAS:VOLT?": ",".join(str(ch * 2) for ch in channels),
        "MEAS:CURR?": ",".join(str(ch / 4) for ch in channels),
    }

    replies.update(
        {
            q: ",".join(value for _ in channels)
            for q, value in (
                ("INP?", "0"),
                ("MODE?", "CURR"),
                ("CURR?", "1"),
                ("VOLT?", "5"),
                ("POW?", "2"),
                ("RES?", "10"),
            )
        }
    )

    class ReadonlyTransport(MockTransport):
        def query(self, command):
            commands.append(command)
            return (replies[command] + "\n").encode("ascii")

        def write(self, command):
            pytest.fail(f"Unexpected write: {command}")

    io = ReadonlyTransport([])
    io.open()
    monkeypatch.setattr(
        "lab_control.application.runtime.create_adapter", lambda config, _visa: build_scpi_adapter(config, io)
    )
    runtime = ApplicationRuntime(root=tmp_path, hardware=False)
    key = runtime.add(ConnectionConfig("mock://load", "duoheyi.dh8318", "mock", expected_serial="TEST0001"))
    runtime.connect(key).result(5)
    window = RealWindow(runtime)
    qtbot.addWidget(window)
    window.show()
    try:
        window.navigate("设备管理", "electronic_load", key)
        page = window.stack.currentWidget()
        assert isinstance(page, ElectronicLoadPage)
        qtbot.waitUntil(lambda: page.frame is not None)
        assert "electronic_load" not in window._submenu_categories("实时监测")
        assert all(spec["device_id"] != key for spec in window.dashboard.specs)
        window.selected_id = key
        window.refresh_details()
        assert window.action_buttons["monitor"].isHidden()
        assert not window.action_buttons["manage"].isHidden()
        window.navigate("实时监测", "electronic_load", key)
        assert window.stack.currentWidget() is page and window.current_module == "仪器测控"
        assert page.table.rowCount() == 8
        assert page.table.verticalScrollBar().maximum() == 0
        visible = [editor for editor in page.editors.values() if editor.isVisible()]
        assert len(visible) == 3
        assert all(not editor.apply_button.isVisible() for editor in page.editors.values())
        assert page.group.apply_button.isVisible() and not page.group.apply_button.isEnabled()
        for ch in range(1, 9):
            row = [page.table.item(ch - 1, col).text() for col in range(1, 6)]
            assert row == (
                ["关闭", "CC", "1 A", f"{ch * 2:g}", f"{ch / 4:g}"] if ch in channels else ["—"] * 5
            )
        page.table.selectRow(6)
        assert page.channel == 7 and all(e.channel == 7 for e in page.editors.values())
        qtbot.waitUntil(page.editors["current"].isVisible)
        assert commands.count("MEAS:VOLT?") == commands.count("MEAS:CURR?") == 1
        assert all(command.endswith("?") for command in commands)
        saved = []
        monkeypatch.setattr(
            runtime.repository, "export", lambda batch, _mode, **kw: (saved.append(batch), tmp_path)[1]
        )
        qtbot.waitUntil(page.save_button.isEnabled)
        page.save_button.click()
        qtbot.waitUntil(lambda: bool(saved))
        assert saved[0].measurements == page.frame.batch.measurements
    finally:
        window.timer.stop()
        window.controller.close()
        runtime.close()
        window.closing = True
        window.bridge.worker.shutdown()
