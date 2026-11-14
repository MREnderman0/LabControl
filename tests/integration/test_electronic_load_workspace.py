import pytest

from lab_control.application.config import ConnectionConfig
from lab_control.application.runtime import ApplicationRuntime
from lab_control.instruments.registry import build_scpi_adapter
from lab_control.transport.base import MockTransport
from lab_control.ui.pages.monitoring.power import PowerMonitoringPage
from lab_control.ui.real_window import RealWindow


@pytest.mark.parametrize("selection,channels", [("ALL", tuple(range(1, 9))), ("6", (6,))])
def test_readonly_load_monitoring_and_export(qtbot, tmp_path, monkeypatch, selection, channels):
    commands = []
    replies = {
        "*IDN?": "DUOHEYI,DH-8318,TEST0001,VER:TEST",
        "CH?": selection,
        "MEAS:VOLT?": ",".join(str(ch * 2) for ch in channels),
        "MEAS:CURR?": ",".join(str(ch / 4) for ch in channels),
    }

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
        window.navigate("实时监测", "electronic_load", key)
        page = window.stack.currentWidget()
        assert isinstance(page, PowerMonitoringPage)
        assert page.controls is None
        assert page.channels() == channels
        assert tuple(page.readouts.rows) == channels
        spec = next(s for s in window.dashboard.specs if s["device_id"] == key)
        assert spec["channels"] == list(channels) and spec["sources"] == ["voltage", "current"]

        page.once_action.trigger()
        qtbot.waitUntil(lambda: page.plot.displayed_frame is not None)
        assert len(page.plot.displayed_frame.batch.measurements) == 2 * len(channels)
        for ch in channels:
            assert page.readouts.rows[ch].text(1) == f"{ch * 2:g}"
            assert page.readouts.rows[ch].text(2) == f"{ch / 4:g}"
        assert commands.count("MEAS:VOLT?") == commands.count("MEAS:CURR?") == 1
        assert all(command.endswith("?") for command in commands)
        saved = []
        monkeypatch.setattr(
            runtime.repository, "export", lambda batch, _mode, **kw: (saved.append(batch), tmp_path)[1]
        )
        qtbot.waitUntil(page.save_action.isEnabled)
        page.save_action.trigger()
        qtbot.waitUntil(lambda: bool(saved))
        assert saved[0].measurements == page.plot.displayed_frame.batch.measurements
    finally:
        window.timer.stop()
        window.controller.close()
        runtime.close()
        window.closing = True
        window.bridge.worker.shutdown()
