"""UI navigation and the real scheduler/writer/replay pipeline, with a scripted wire."""

import time

import numpy as np

from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.connections import ConnectionConfig
from lab_control.domain.instruments import AcquisitionRequest, DeviceCommand
from lab_control.instruments.devices.liquid_moku_pro.adapter import ROLES, Adapter
from lab_control.instruments.devices.liquid_moku_pro.protocol import ADAPTER
from lab_control.storage.recording import read_frames

from .test_protocol import Wire


def connected(root, wire=None):
    rt = ApplicationRuntime(root=root, hardware=False)
    wire = Wire() if wire is None else wire
    default_factory = rt.sessions.factory
    rt.sessions.factory = lambda config: (
        Adapter(config, wire) if config.adapter == ADAPTER else default_factory(config)
    )
    key = rt.add(ConnectionConfig("mock://four-slots", ADAPTER, "mock", expected_serial="000088"))
    rt.connect(key).result(4)
    return rt, key, wire


def wait(predicate):
    deadline = time.monotonic() + 10
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(0.01)


def test_one_session_dual_record_stop_replay_export(tmp_path):
    rt, key, wire = connected(tmp_path)
    with rt:
        rt.monitoring.apply(key, DeviceCommand("attach_workspace", confirmed=True)).result(3)
        for kind in ("waveform", "spectrum"):
            future = rt.monitoring.start_role(key, AcquisitionRequest(kind), recording=True)
            if future:
                future.result(4)
        wait(lambda: all(rt.monitoring.latest(key, k) for k in ("waveform", "spectrum")))
        previous = rt.monitoring.latest(key, "spectrum").sequence
        rt.monitoring.stop_role(key, "waveform")
        wait(lambda: rt.monitoring.latest(key, "spectrum").sequence > previous)
        assert rt.sessions.sessions[key].recording
        rt.monitoring.stop_role(key, "spectrum").result(15)
        assert len(rt.sessions.sessions) == 1 and wire.key is not None
        assert sum(p == "moku/claim_ownership" for _, p, _ in wire.calls) == 1
        segments = list(tmp_path.rglob("segment-*.sqlite3"))
        assert segments
        frames = [f for p in segments for f in read_frames(p)]
        samples = [f["measurements"][0] for f in frames]
        assert {s["metadata"]["channel_label"] for s in samples} == {"示波器 ch1", "频谱仪 ch1"}
        for s in samples:
            if s["axis_unit"] == "s":
                assert np.array_equal(s["values"], [0, 0.05, 0])
            else:
                assert np.array_equal(s["axis"], [0, 10000, 20000]) and s["unit"] == "dBm"
        csv = [p.read_text(encoding="utf-8-sig").splitlines()[0] for p in tmp_path.rglob("*.csv")]
        for label in ("示波器 ch1", "频谱仪 ch1"):
            assert any(label in h for h in csv)
    assert wire.key is None


def test_last_recording_role_drains_while_peer_keeps_monitoring(tmp_path):
    rt, key, _ = connected(tmp_path)
    with rt:
        rt.monitoring.apply(key, DeviceCommand("attach_workspace", confirmed=True)).result(3)
        rt.monitoring.start_role(key, AcquisitionRequest("spectrum"))
        rt.monitoring.start_role(key, AcquisitionRequest("waveform"), recording=True).result(4)
        wait(lambda: rt.monitoring.latest(key, "waveform"))
        with rt.monitoring.lock:
            rt.monitoring.stop_role(key, "waveform")
            snap = rt.monitoring.snapshots()[key]
            assert snap["running"] and snap["kinds"] == ("spectrum",)
            assert not snap["recording_state"]["binary_done"] and not snap["recording_kinds"]
            assert snap["recorder_kinds"] == ("waveform",)
        wait(lambda: rt.monitoring.snapshots()[key]["recording_state"]["binary_done"])
        rt.monitoring.start_role(key, AcquisitionRequest("spectrum"), recording=True).result(4)
        assert rt.monitoring.snapshots()[key]["recorder_kinds"] == ("spectrum",)
        rt.monitoring.stop_role(key, "spectrum").result(15)


def test_three_role_pages_never_start_output(qtbot, tmp_path):
    from lab_control.instruments.devices.liquid_moku_pro.workspace import RolePage
    from lab_control.ui.pages.monitoring.oscilloscope_plot import OscilloscopePlot
    from lab_control.ui.pages.monitoring.spectrum_markers import SpectrumPlot
    from lab_control.ui.real_window import RealWindow

    rt, key, wire = connected(tmp_path)
    window = RealWindow(rt)
    qtbot.addWidget(window)
    try:
        window.show()
        for index, role in enumerate(ROLES):
            window.navigate("仪器测控", role, key)
            panel = window.stack.currentWidget()
            assert isinstance(panel, RolePage) and panel.role == role
            assert panel.selector.combo.currentData() == key
            if role == "signal_generator":
                assert tuple(panel.cards) == (1, 2, 3, 4)
                panel.cards[4].editors["frequency"].set_value(25000)
                assert panel.cards[1].editors["frequency"].value() == 10000
            else:
                assert isinstance(panel.plot, OscilloscopePlot if role == "oscilloscope" else SpectrumPlot)
                assert panel.splitter.count() == 2
            assert [panel.channels.itemText(n) for n in range(4)] == ["ch1", "ch2", "ch3", "ch4"]
            panel.channels.setCurrentIndex(index + 1)
        for index, role in enumerate(ROLES):
            window.navigate("仪器测控", role, key)
            assert window.stack.currentWidget().channel == index + 2
        window.navigate("数据处理", "single_device")
        window.stack.currentWidget().open_device_requested.emit(key, 3, "spectrum_analyzer")
        assert window.stack.currentWidget().role == "spectrum_analyzer"
        assert window.stack.currentWidget().channel == 3
        assert not wire.calls and len(rt.sessions.sessions) == 1
        peer = rt.add(ConnectionConfig("mock://peer-source", "rs.sma100b", "mock"))
        rt.connect(peer).result(4)
        window.controller.poll()
        window.navigate("仪器测控", "signal_generator", key)
        generator = window.stack.currentWidget()
        generator.selector.combo.setCurrentIndex(generator.selector.combo.findData(peer))
        ordinary = window.stack.currentWidget()
        assert ordinary is not generator and ordinary.device_id == peer
        ordinary.selector.combo.setCurrentIndex(ordinary.selector.combo.findData(key))
        assert window.stack.currentWidget() is generator and generator.channel == 2
        assert not wire.calls
    finally:
        window.timer.stop()
        window.controller.close()
        rt.close()
        window.closing = True
        window.bridge.worker.shutdown()


def test_buttons_prepare_selected_ports_and_join_recording_without_writes(qtbot, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox, QPushButton

    from lab_control.ui.real_window import RealWindow

    rt, key, wire = connected(tmp_path)
    del wire.routes["Output4"]
    window = RealWindow(rt)
    qtbot.addWidget(window)

    def panel(role):
        window.navigate("仪器测控", role)
        return window.stack.currentWidget()

    def click(page, title):
        next(b for b in page.findChildren(QPushButton) if b.text() == title).click()

    def until(fn):
        qtbot.waitUntil(lambda: bool(fn()), timeout=6000)

    try:
        window.show()
        spectrum = panel("spectrum_analyzer")
        spectrum.channels.setCurrentIndex(2)
        monkeypatch.setattr(QMessageBox, "question", lambda *_: QMessageBox.StandardButton.No)
        click(spectrum, "开始监测")
        assert not wire.calls
        monkeypatch.setattr(QMessageBox, "question", lambda *_: QMessageBox.StandardButton.Yes)
        click(spectrum, "逐帧记录")
        until(lambda: spectrum.last and spectrum.last.request.channels == (3,))
        assert "20 kHz" in spectrum.settings.text() and "Hann" in spectrum.settings.text()
        assert spectrum.plot.trace.unit == "dBm"
        assert wire.routes["Slot3InA"] == "Input3"
        scope = panel("oscilloscope")
        scope.channels.setCurrentIndex(1)
        writes = [v for _, p, v in wire.calls if p == "mim/set_connections"]
        click(scope, "开始监测")
        until(lambda: "不能更改输入路由" in scope.status.text())
        scope.channels.setCurrentIndex(0)
        click(scope, "开始监测")
        until(lambda: scope.last and scope.last.request.channels == (1,))
        assert "1 MSa/s" in scope.settings.text()
        assert scope.plot.displayed_frame.batch.measurements[0].axis_unit == "s"
        # Every role page shows all three roles; only the recording panel gets the red frame.
        scope.refresh_status()
        chips = scope.role_bar.chips.group.buttons()
        assert [c.text() for c in chips] == [
            "信号源 · 无输出回执",
            "示波器 · ch1 采集中",
            "频谱仪 · ch3 记录中",
        ]
        assert [c.isChecked() for c in chips] == [False, True, False]
        assert scope.role_bar.note.text() == ""
        assert scope.chart.property("recordingPhase") == "idle"
        assert panel("spectrum_analyzer").chart.property("recordingPhase") == "recording"
        scope = panel("oscilloscope")
        assert [v for _, p, v in wire.calls if p == "mim/set_connections"] == writes
        assert not any(p.endswith("/generate_waveform") for _, p, _ in wire.calls)
        previous = rt.monitoring.latest(key, "spectrum").sequence
        click(scope, "停止本面板")
        until(lambda: rt.monitoring.latest(key, "spectrum").sequence > previous)
        spectrum = panel("spectrum_analyzer")
        with rt.monitoring.lock:
            click(spectrum, "停止本面板")
            assert not rt.monitoring.snapshots()[key]["recording_state"]["binary_done"]
            assert spectrum.chart.property("recordingPhase") == "draining"
            assert spectrum.record_button.text() == "收尾中" and not spectrum.record_button.isEnabled()
            assert spectrum.role_bar.chips.group.button(2).text() == "频谱仪 · 收尾中"
            assert panel("oscilloscope").chart.property("recordingPhase") == "idle"
        spectrum = panel("spectrum_analyzer")
        until(lambda: spectrum.chart.property("recordingPhase") == "idle")
        until(lambda: not rt.sessions.sessions[key].controls_locked)
        generator = panel("signal_generator")
        generator.channels.setCurrentIndex(3)
        until(generator.cards[4].on.isEnabled)
        click(generator.cards[4], "开启输出")
        until(lambda: any(p.endswith("/generate_waveform") for _, p, _ in wire.calls))
        waves = [v for _, p, v in wire.calls if p.endswith("/generate_waveform")]
        assert len(waves) == 1 and waves[0]["channel"] == 1 and waves[0]["type"] == "Sine"
        # The acknowledged output shows on its card and in the role bar of every role page.
        until(generator.cards[4].output.isVisible)
        assert generator.cards[4].output.label.text() == "输出中"
        assert not generator.cards[1].output.isVisible()
        assert generator.role_bar.chips.group.buttons()[0].text() == "信号源 · CH4 输出"
        calls = len(wire.calls)
        generator.role_bar.chips.group.buttons()[1].click()
        scope = window.stack.currentWidget()
        assert scope.role == "oscilloscope" and scope.role_bar.chips.group.buttons()[1].isChecked()
        assert scope.role_bar.chips.group.buttons()[0].text() == "信号源 · CH4 输出"
        assert generator.role_bar.chips.group.buttons()[0].isChecked()
        assert len(wire.calls) == calls
        assert wire.routes["Output4"] == "Slot2OutA" and wire.routes["Slot4InA"] == "Input4"
        assert sum(p == "moku/claim_ownership" for _, p, _ in wire.calls) == 1
    finally:
        window.timer.stop()
        window.controller.close()
        rt.close()
        window.closing = True
        window.bridge.worker.shutdown()
