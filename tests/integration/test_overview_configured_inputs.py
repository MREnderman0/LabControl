"""Exercise one-click sampling with and without an existing PicoSDK input configuration."""

import pytest

from lab_control.application.config import ConnectionConfig
from lab_control.application.runtime import ApplicationRuntime
from lab_control.demo.instrument import SimulatedInstrument
from lab_control.domain.instruments import DeviceStatus
from lab_control.ui.real_window import RealWindow


@pytest.mark.parametrize("configured", [False, True])
def test_one_click_reads_only_session_enabled_channels(qtbot, tmp_path, monkeypatch, configured):
    runtime = ApplicationRuntime(root=tmp_path, hardware=False)
    runtime.sessions.factory = SimulatedInstrument
    key = runtime.add(ConnectionConfig("mock://overview/pico", "pico.6404e", "mock"))
    writes = []
    inputs = {1: {"enabled": False}, 2: {"enabled": True}, 4: {"enabled": True}} if configured else {}
    monkeypatch.setattr(
        SimulatedInstrument, "read_status", lambda self: DeviceStatus({"configured_channels": inputs})
    )
    monkeypatch.setattr(SimulatedInstrument, "apply", lambda self, command: writes.append(command))
    runtime.connect(key, configure_acquisition=False).result(3)
    window = RealWindow(runtime)
    qtbot.addWidget(window)
    try:
        runtime.monitoring.enable_settings_sync(False)
        window.navigate("实时监测")
        card = window.dashboard.cards[key]
        card.enabled.setChecked(True)
        window.dashboard.sample_button.click()
        if configured:
            qtbot.waitUntil(lambda: key in runtime.monitoring.tasks)
            task = runtime.monitoring.tasks[key]
            assert task.requests[0].channels == (2, 4)
            assert task.requests[0].allow_setup is False
            assert task.requests[0].initiate and task.requests[0].confirmed
            qtbot.waitUntil(lambda: runtime.monitoring.latest(key, "waveform") is not None)
            runtime.monitoring.stop(key).result(5)
        else:
            qtbot.waitUntil(lambda: card.state.text() == "未开始")
            assert "没有已配置并启用的通道" in card.detail_state.text()
            assert key not in runtime.monitoring.tasks
            assert not list(tmp_path.rglob("raw.bin"))
        assert writes == []
    finally:
        window.timer.stop()
        window.controller.close()
        runtime.close()
        window.bridge.worker.shutdown(wait=True)
        window.closing = True
        window.close()


@pytest.mark.parametrize(
    "adapter", ["rs.rtm3004", "rs.rto64", "rigol.ds1202ze", "lecroy.9254", "lecroy.9404m", "rigol.dho924"]
)
def test_scope_overview_uses_enabled_channel_from_instrument(qtbot, tmp_path, monkeypatch, adapter):
    from lab_control.domain.instruments import ParameterReading

    runtime = ApplicationRuntime(root=tmp_path, hardware=False)
    runtime.sessions.factory = SimulatedInstrument
    key = runtime.add(ConnectionConfig("mock://overview/secondary-only", adapter, "mock"))
    original = SimulatedInstrument.read_settings
    writes = []

    def readings(device, operations, channel=1):
        if tuple(operations) == ("channel_enabled",):
            return (ParameterReading("channel_enabled", channel, channel == 2, "simulated"),)
        return original(device, operations, channel)

    monkeypatch.setattr(SimulatedInstrument, "read_settings", readings)
    monkeypatch.setattr(SimulatedInstrument, "apply", lambda self, command: writes.append(command))
    runtime.connect(key, configure_acquisition=False).result(3)
    window = RealWindow(runtime)
    qtbot.addWidget(window)
    try:
        runtime.monitoring.enable_settings_sync(False)
        window.navigate("实时监测")
        window.dashboard.sample_button.click()
        qtbot.waitUntil(lambda: key in runtime.monitoring.tasks)
        task = runtime.monitoring.tasks[key]
        assert task.requests[0].channels == (2,)
        assert task.requests[0].allow_setup is False
        qtbot.waitUntil(lambda: runtime.monitoring.latest(key, "waveform") is not None)
        runtime.monitoring.stop(key).result(5)
        assert not writes
    finally:
        window.timer.stop()
        window.controller.close()
        runtime.close()
        window.bridge.worker.shutdown(wait=True)
        window.closing = True
        window.close()


def test_multirow_overview_keeps_controls_inside_cards(qtbot, tmp_path):
    runtime = ApplicationRuntime(root=tmp_path, hardware=False)
    for index in range(5):
        key = runtime.add(ConnectionConfig(f"mock://grid/{index}", "rigol.dsa815", "mock"))
        runtime.connect(key, configure_acquisition=False).result(3)
    window = RealWindow(runtime)
    qtbot.addWidget(window)
    try:
        window.resize(1200, 720)
        window.show()
        window.navigate("实时监测")
        grid = window.dashboard.groups["spectrum_analyzer"].grid
        qtbot.waitUntil(lambda: 0 < grid.columns < len(grid.items))
        for card in grid.items:
            assert card.height() >= card.sizeHint().height()
            assert card.enabled.geometry().bottom() < card.state.geometry().top()
            assert card.state.geometry().bottom() < card.rect().bottom()
        rows = sorted(set(c.y() for c in grid.items))
        assert rows[1] >= rows[0] + grid.items[0].height()
    finally:
        window.timer.stop()
        window.controller.close()
        runtime.close()
        window.bridge.worker.shutdown(wait=True)
        window.closing = True
        window.close()


@pytest.mark.parametrize("cancel", [True, False])
def test_pending_input_check_can_cancel_or_skip_one_device(qtbot, tmp_path, monkeypatch, cancel):
    from concurrent.futures import Future

    runtime = ApplicationRuntime(root=tmp_path, hardware=False)
    runtime.sessions.factory = SimulatedInstrument
    pico = runtime.add(ConnectionConfig("mock://overview/pending", "pico.6404e", "mock"))
    meter = runtime.add(ConnectionConfig("mock://overview/other", "joinwit.jw8105", "mock"))
    for device in (pico, meter):
        runtime.connect(device, configure_acquisition=False).result(3)
    window = RealWindow(runtime)
    qtbot.addWidget(window)
    try:
        runtime.monitoring.enable_settings_sync(False)
        window.navigate("实时监测")
        pending = Future()
        submit = runtime.sessions.submit

        def hold_status(key, operation, *args, **kwargs):
            if key == pico and operation == "status":
                return pending
            return submit(key, operation, *args, **kwargs)

        monkeypatch.setattr(runtime.sessions, "submit", hold_status)
        window.dashboard.sample_button.click()
        assert not runtime.monitoring.tasks
        if cancel:
            window.dashboard.sample_button.click()
        pending.set_result(DeviceStatus({"configured_channels": {}}))
        qtbot.waitUntil(lambda: not any(job[0] is pending for job in window.controller.jobs))
        if cancel:
            assert not runtime.monitoring.tasks
        else:
            assert meter in runtime.monitoring.tasks, runtime.monitoring.snapshots()
            task = runtime.monitoring.tasks[meter]
            request = task.requests[0]
            qtbot.waitUntil(
                lambda: runtime.monitoring.latest(meter, request.kind, request.quantity) is not None
            )
            assert window.dashboard.cards[pico].state.text() == "未开始"
            assert pico not in runtime.monitoring.tasks
            runtime.monitoring.stop(meter).result(5)
    finally:
        window.timer.stop()
        window.controller.close()
        runtime.close()
        window.bridge.worker.shutdown(wait=True)
        window.closing = True
        window.close()
