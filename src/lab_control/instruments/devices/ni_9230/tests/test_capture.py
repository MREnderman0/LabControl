import ctypes as ct
from dataclasses import replace

import numpy as np
import pytest
from PySide6.QtWidgets import QPushButton

from lab_control.application.controls import data_sources
from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.connections import ConnectionConfig
from lab_control.domain.instruments import DeviceError
from lab_control.instruments.devices.ni_9230 import adapter, daqmx
from lab_control.ui.pages.monitoring.acceleration import AccelerationMonitoringPage
from lab_control.ui.real_window import RealWindow


class Fake:
    def __init__(self):
        self.calls = []
        self.bad = ["Mod/ai0"]
        self.scan_index = 0
        self.available = 0
        self.offsets = np.zeros(3)

    def modules(self):
        return {"00000001": "Mod"}

    def number(self, name, dtype, *args):
        assert name == "GetPhysicalChanAITermCfgs"
        return 1

    def call(self, name, *args):
        self.calls.append(name)
        if name == "CreateTask":
            ct.cast(args[1], ct.POINTER(daqmx.HANDLE))[0] = daqmx.HANDLE(1)

    def start_input(self, module, channels, handle, gains):
        self.calls.append("start")
        assert gains == [19300] * len(channels)
        return tuple(f"{module}/ai{c - 1}" for c in channels)

    def read_point(self, handle, count):
        self.scan_index += 1
        return self.scan_index, np.array([[0.001], [-0.015], [0.025]])[:count]

    def read_window(self, handle, count, points):
        if self.available < points:
            return None
        self.scan_index = max(self.scan_index, self.available - 1)
        return self.available - 1, np.repeat(self.offsets[:count, None], points, axis=1)

    def disable_excitation(self, names):
        self.calls.append(("off", names))

    def faults(self, handle):
        return {"OpenCurrentLoop": self.bad, "Overcurrent": []}


def test_repeated_points_share_one_task_and_omit_faults(monkeypatch):
    drv = Fake()
    monkeypatch.setattr(adapter, "Driver", lambda: drv)
    cfg = ConnectionConfig("NIDAQMX::00000001", adapter.ID, "nidaqmx", options=("ct12000l.all=19300",))
    dev = adapter.Adapter(cfg)
    assert dev.identify().serial == "00000001" and not drv.calls
    source = data_sources(cfg)[0]
    req = replace(source.request((1, 2, 3), 1, True, True), context_id="run-a")
    with pytest.raises(DeviceError):
        dev.acquire(req)
    with pytest.raises(DeviceError):
        dev.prepare_monitor_input(replace(req, initiate=False, confirmed=False))
    with pytest.raises(DeviceError, match="稳定") as warm:
        dev.prepare_monitor_input(req)
    assert warm.value.code == "monitor_wait"
    drv.available = adapter.WINDOW_POINTS
    drv.offsets[2] = 0.02
    dev.prepare_monitor_input(req)
    early = dev.acquire(req)
    assert [m.channel for m in early.measurements] == [2]
    assert early.configuration["channel_status"] == {
        "ai0": "IEPE 开路",
        "ai1": "正常",
        "ai2": adapter.WAITING,
    }
    drv.offsets[2] = 0
    drv.available += adapter.WINDOW_POINTS
    for _ in range(3):
        dev.prepare_monitor_input(req)
        batch = dev.acquire(req)
        assert [m.channel for m in batch.measurements] == [2, 3]
        assert [m.values.tolist() for m in batch.measurements] == [[-0.015], [0.025]]
        assert all(m.unit == "g" and m.axis is None for m in batch.measurements)
        assert batch.configuration["channel_status"]["ai0"] == "IEPE 开路"
    drv.scan_index -= 1
    with pytest.raises(DeviceError) as stale:
        dev.acquire(req)
    assert stale.value.code == "monitor_no_signal"
    drv.bad = ["Mod/ai0", "Mod/ai1", "Mod/ai2"]
    with pytest.raises(DeviceError) as missing:
        dev.acquire(req)
    assert missing.value.code == "monitor_no_signal"
    drv.bad = []
    with pytest.raises(DeviceError):
        dev.prepare_monitor_input(req)
    drv.available = drv.scan_index + adapter.WINDOW_POINTS + 2
    dev.prepare_monitor_input(req)
    assert len(dev.acquire(req).measurements) == 3
    dev.end_monitor_input()
    dev.close()
    assert drv.calls == ["CreateTask", "start", "ClearTask", ("off", ("Mod/ai0", "Mod/ai1", "Mod/ai2"))]


def test_continuous_page_arms_once_and_releases_after_stop(qtbot, tmp_path, monkeypatch):
    drv = Fake()
    monkeypatch.setattr(adapter, "Driver", lambda: drv)
    drv.available = adapter.WINDOW_POINTS
    cfg = ConnectionConfig("NIDAQMX::00000001", adapter.ID, "nidaqmx", options=("ct12000l.all=19300",))
    with ApplicationRuntime(root=tmp_path, hardware=False) as runtime:
        runtime.sessions.factory = lambda _: adapter.Adapter(cfg)
        key = runtime.add(ConnectionConfig("mock://scalar-page", adapter.ID, "mock"))
        runtime.connect(key, configure_acquisition=False).result(5)
        window = RealWindow(runtime)
        qtbot.addWidget(window)
        try:
            window.show()
            window.navigate("实时监测", "accelerometer", key)
            page = window.stack.currentWidget()
            assert isinstance(page, AccelerationMonitoringPage)
            qtbot.waitUntil(page.start_button.isEnabled, timeout=5000)
            assert not drv.calls and page.channels() == (1, 2, 3)
            assert not any("单次" in b.text() for b in page.findChildren(QPushButton))
            page.timing.interval.setValue(0.5)
            page.start_button.click()
            task = runtime.monitoring.tasks[key]
            qtbot.waitUntil(lambda: page.cards[1].status.text() == "IEPE 开路", timeout=12000)
            assert page.cards[2].value.text() == "-0.015 g"
            assert task.requests[0].points == 1 and not task.once
            page.record_button.click()
            qtbot.waitUntil(
                lambda: bool(task.recorder and task.recorder.snapshot()["committed"] >= 2), timeout=10000
            )
            page.record_button.click()
            qtbot.waitUntil(lambda: task.recorder.finished.done(), timeout=10000)
            assert task.running and "ClearTask" not in drv.calls
            page.start_button.click()
            runtime.monitoring.stop(key).result(5)
            assert drv.calls == [
                "CreateTask",
                "start",
                "ClearTask",
                ("off", ("Mod/ai0", "Mod/ai1", "Mod/ai2")),
            ]
        finally:
            window.timer.stop()
            window.controller.close()
            window.bridge.worker.shutdown(wait=True)
            window.closing = True
            window.close()


def test_startup_checks_offset_and_drift_without_rejecting_vibration():
    t = np.arange(adapter.WINDOW_POINTS) / daqmx.RATE
    vibration = 0.04 * np.sin(2 * np.pi * 5 * t)
    assert adapter.stable_baseline(vibration)
    assert not adapter.stable_baseline(vibration + 0.02)
    assert not adapter.stable_baseline(vibration + np.linspace(-0.0004, 0.0004, t.size))
    vibration[-1] = 0.26
    assert not adapter.stable_baseline(vibration)
