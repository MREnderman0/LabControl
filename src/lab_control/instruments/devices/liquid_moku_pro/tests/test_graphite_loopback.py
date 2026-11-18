"""Exercise the real graphite UI against a simulated CH1 output-to-input cable."""

import json

import numpy as np
import pytest
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QMessageBox

from lab_control.app import load_stylesheet, load_ui_font
from lab_control.ui.real_window import RealWindow

from .test_protocol import Wire
from .test_workspace import connected


class LoopbackWire(Wire):
    def __init__(self):
        super().__init__()
        self.outputs = {}
        self.t = np.arange(10000) / 1e6
        self.noise = np.random.default_rng(88).normal(0, 3e-6, self.t.size)

    def call(self, method, path, params=None):
        if path.endswith("/generate_waveform"):
            response = super().call(method, path, params)
            self.outputs[params["channel"]] = response
            return response
        if path.endswith("/get_span"):
            return {"frequency1": 0, "frequency2": 30000}
        if not path.endswith("/get_data"):
            return super().call(method, path, params)

        self.calls.append((method, path, params))
        assert self.key and params["wait_complete"]
        scope = path.startswith("slot1/")
        logical = "ABCD".index(self.routes["Output1"][-1]) + 1
        output = self.outputs.get(logical, {})
        signal = self.noise.copy()
        if self.routes["Slot1InA" if scope else "Slot3InA"] == "Input1" and output.get("type") == "Sine":
            signal += 0.5 * output["amplitude"] * np.sin(2 * np.pi * output["frequency"] * self.t)
        if scope:
            return {"time": self.t[:501], "ch2": signal[:501]}
        window = np.hanning(signal.size)
        peak_volts = 2 * np.abs(np.fft.rfft(signal * window)) / window.sum()
        dbm = 10 * np.log10(np.maximum(peak_volts**2 / (2 * 50) * 1000, 1e-18))
        frequencies = np.fft.rfftfreq(signal.size, d=1e-6)
        keep = frequencies <= 30000
        return {"frequency": frequencies[keep], "ch1": dbm[keep]}


def test_graphite_category_to_ch1_loopback(qtbot, tmp_path, monkeypatch):
    wire = LoopbackWire()
    runtime, device, _ = connected(tmp_path / "runtime", wire)
    window = RealWindow(runtime)
    qtbot.addWidget(window)
    window.setStyleSheet(load_stylesheet())
    window.setFont(QFont(load_ui_font(), 10))
    window.resize(1430, 900)
    monkeypatch.setattr(QMessageBox, "question", lambda *_: QMessageBox.StandardButton.Yes)

    def wait(fn):
        qtbot.waitUntil(lambda: bool(fn()), timeout=8000)

    def open_role(role):
        window.navigate("首页")
        window.cards[role].clicked.emit(role)
        window.open_details(device)
        assert not window.rows[device].isHidden()
        action = window.action_buttons["manage"]
        assert action.isVisible() and action.isEnabled()
        action.click()
        page = window.stack.currentWidget()
        assert page.role == role and page.selector.combo.currentData() == device
        assert window.chrome.category_buttons[role].isChecked()
        return page

    def capture(page, filename):
        window.repaint()
        assert window.stack.currentWidget() is page
        assert window.grab().save(str(tmp_path / (filename + ".png")))

    def stop(page):
        page.stop_button.click()
        wait(lambda: not runtime.sessions.sessions[device].controls_locked)

    results = []
    try:
        window.show()
        source = open_role("signal_generator")
        assert not wire.calls
        card = source.cards[1]
        for frequency in (10000, 20000):
            source = open_role("signal_generator")
            card.editors["frequency"].set_value(frequency)
            card.on.click()
            wait(lambda: wire.outputs.get(4, {}).get("frequency") == frequency)
            wait(lambda: "已确认" in source.status.text())
            if frequency == 10000:
                capture(source, "signal-generator")
            scope = open_role("oscilloscope")
            scope.record_button.click()
            wait(lambda: scope.last and scope.record_button.text() == "记录中")
            trace = scope.last.batch.measurements[0]
            vpp = float(np.ptp(trace.values))
            crossing = np.flatnonzero((trace.values[:-1] <= 0) & (trace.values[1:] > 0))
            period = float(np.median(np.diff(trace.axis[crossing])))
            assert vpp == pytest.approx(0.1, abs=0.0003)
            assert period == pytest.approx(1 / frequency, abs=1.1e-6)
            if frequency == 10000:
                capture(scope, "oscilloscope")
            spectrum = open_role("spectrum_analyzer")
            spectrum.record_button.click()
            wait(lambda: spectrum.last and spectrum.record_button.text() == "记录中")
            trace = spectrum.last.batch.measurements[0]
            index = int(np.argmax(trace.values))
            peak, level = float(trace.axis[index]), float(trace.values[index])
            assert peak == frequency and level == pytest.approx(-16.02, abs=0.05)
            if frequency == 10000:
                capture(spectrum, "spectrum-analyzer")
            results.append(
                dict(set_hz=frequency, measured_vpp=vpp, period_us=period * 1e6, peak_hz=peak, peak_dbm=level)
            )
            scope = open_role("oscilloscope")
            scope.stop_button.click()
            wait(lambda: not scope.stop_button.isEnabled())
            spectrum = open_role("spectrum_analyzer")
            previous = spectrum.last.sequence
            wait(lambda: spectrum.last.sequence > previous)
            stop(spectrum)

        source = open_role("signal_generator")
        card.off.click()
        wait(lambda: wire.outputs[4]["type"] == "Off")
        scope = open_role("oscilloscope")
        scope.monitor_button.click()
        wait(lambda: scope.last and np.ptp(scope.last.batch.measurements[0].values) < 50e-6)
        off_vpp = float(np.ptp(scope.last.batch.measurements[0].values))
        stop(scope)
        assert set(wire.outputs) == {4} and wire.routes["Output1"] == "Slot2OutD"
        (tmp_path / "loopback.json").write_text(
            json.dumps({"source": "simulation", "ch1": results, "off_vpp": off_vpp}, indent=2),
            encoding="utf-8",
        )
    finally:
        window.timer.stop()
        window.controller.close()
        window.agent_panel.shutdown()
        window.environment_panel.shutdown()
        window.storage_panel.shutdown()
        window.closing = True
        window.close()
        window.bridge.worker.shutdown(wait=True)
        runtime.close()
