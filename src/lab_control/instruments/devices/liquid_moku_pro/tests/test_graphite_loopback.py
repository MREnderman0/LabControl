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
        self.settings["span"]["frequency2"] = 30000
        self.t = np.arange(10000) / 1e6
        self.noise = np.random.default_rng(88).normal(0, 3e-6, self.t.size)

    def call(self, method, path, params=None):
        if path.endswith("/generate_waveform"):
            response = super().call(method, path, params)
            self.outputs[params["channel"]] = response
            return response
        if not path.endswith("/get_data"):
            return super().call(method, path, params)

        self.calls.append((method, path, params))
        assert self.key and params["wait_complete"]
        scope = path.startswith("slot1/")
        logical = "ABCD".index(self.routes["Output1"][-1]) + 1
        output = self.outputs.get(logical, {})
        if scope:
            timing = self.settings["timebase"]
            count = min(self.max_length, 1 + round((timing["t2"] - timing["t1"]) * 1e6))
            times = np.linspace(timing["t1"], timing["t2"], count)
        else:
            times = self.t
        signal = np.resize(self.noise, times.size)
        if self.routes["Slot1InA" if scope else "Slot3InA"] == "Input1" and output.get("type") == "Sine":
            amplitude = 0.5 * output["amplitude"]
            phase = 0.0
            trigger = self.settings.get("trigger", {})
            if scope and trigger.get("source") == "ChannelB" and abs(trigger["level"]) <= amplitude:
                phase = np.arcsin(trigger["level"] / amplitude)
                if trigger["edge"] == "Falling":
                    phase = np.pi - phase
            signal += amplitude * np.sin(2 * np.pi * output["frequency"] * times + phase)
        if scope:
            return {"time": times, "ch2": signal}
        window_fn = {"Hann": np.hanning, "Hamming": np.hamming}[self.settings["window"]["window"]]
        window = window_fn(signal.size)
        rbw = self.settings["rbw"]
        if rbw["mode"] == "Manual":
            # Choose an FFT record length by the window's equivalent noise bandwidth.
            enbw_hz = 1e6 * (window @ window) / window.sum() ** 2
            length = int(np.clip(round(signal.size * enbw_hz / rbw["value"]), 4, signal.size))
            window = window_fn(length)
        peak_volts = 2 * np.abs(np.fft.rfft(signal[: window.size] * window, n=signal.size)) / window.sum()
        dbm = 10 * np.log10(np.maximum(peak_volts**2 / (2 * 50) * 1000, 1e-18))
        frequencies = np.fft.rfftfreq(signal.size, d=1e-6)
        span = self.settings["span"]
        keep = (frequencies >= span["frequency1"]) & (frequencies <= span["frequency2"])
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

    def configure(page, group, **values):
        controls = page.capture
        controls.groups.setCurrentIndex(controls.groups.findData(group))
        for key, val in values.items():
            editor = controls.editors[group][key]
            if editor.spec.kind == "enum":
                editor.input.setCurrentIndex(editor.input.findData(val))
            else:
                editor.set_value(val)
        wait(controls.apply_button.isEnabled)
        before = controls.feedback.text()
        controls.apply_button.click()
        wait(lambda: controls.feedback.text() and controls.feedback.text() != before)
        return controls.feedback.text()

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
            if frequency == 10000:
                with monkeypatch.context() as cancel:
                    cancel.setattr(QMessageBox, "question", lambda *_: QMessageBox.StandardButton.No)
                    count = len(wire.calls)
                    scope.capture.apply_button.click()
                    assert len(wire.calls) == count
                bandwidth = scope.front_fields["bandwidth"]
                bandwidth.setCurrentIndex(bandwidth.findData("600MHz"))
                scope.apply_front.click()
                wait(lambda: "600MHz" in scope.readback.text())
                assert "设置回执" in configure(
                    scope, "scope_trigger", mode="AUTO", edge="FALLING", level=0.01
                )
                assert wire.settings["trigger"]["source"] == "ChannelB"
                assert "Precision" in configure(scope, "scope_acquisition", mode="PRECISION")
                assert "Linear" in configure(scope, "scope_interpolation", interpolation="LINEAR")
                assert "设备读回" in configure(scope, "scope_timebase", t1=-0.0002, t2=0.0002, max_length=512)
                scope.monitor_button.click()
                wait(lambda: scope.last and "Precision / Linear" in scope.settings.text())
                measurement = scope.last.batch.measurements[0]
                np.testing.assert_allclose(measurement.axis[[0, -1]], [-0.0002, 0.0002])
                zero = np.argmin(abs(measurement.axis))
                assert measurement.values[zero] == pytest.approx(0.01, abs=15e-6)
                assert measurement.values[zero - 1] > measurement.values[zero + 1]
                capture(scope, "scope-settings")
                # A new frame must leave an unfinished edit intact.
                editor = scope.capture.editors["scope_timebase"]["t1"]
                editor.input.setText("-125")
                editor.input.textEdited.emit("-125")
                sequence = scope.last.sequence
                wait(lambda: scope.last.sequence > sequence)
                assert editor.input.text() == "-125"
                assert wire.settings["timebase"]["t1"] == -0.0002
                scope.stop_button.click()
                wait(lambda: not scope.stop_button.isEnabled())
                spectrum = open_role("spectrum_analyzer")
                assert "600 MHz" in configure(spectrum, "spectrum_span", frequency1=300e6, frequency2=600e6)
                assert wire.settings["span"]["frequency2"] == 600e6
                wideband_readback = wire.settings["span"].copy()
                assert "Hamming" in configure(spectrum, "spectrum_window", window="HAMMING")
                assert "设置回执" in configure(spectrum, "spectrum_averaging", target_duration=0.2)
                assert "设备读回" in configure(spectrum, "spectrum_center", center=15000, span=20000)
                assert "300 Hz" in configure(spectrum, "spectrum_rbw", mode="MANUAL", rbw_value=275)
                spectrum.monitor_button.click()
                wait(lambda: spectrum.last and "300 Hz" in spectrum.settings.text())
                assert "600MHz" in spectrum.readback.text()
                np.testing.assert_allclose(spectrum.last.batch.measurements[0].axis[[0, -1]], [5000, 25000])
                capture(spectrum, "spectrum-settings")
                spectrum.stop_button.click()
                wait(lambda: not spectrum.stop_button.isEnabled())
                scope = open_role("oscilloscope")
            scope.record_button.click()
            wait(lambda: scope.last and scope.record_button.text() == "记录中")
            assert not scope.capture.apply_button.isEnabled()
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
            json.dumps(
                {
                    "source": "simulation",
                    "ch1": results,
                    "off_vpp": off_vpp,
                    "ch1_frontend": wire.front[1],
                    "wideband_span_readback_hz": wideband_readback,
                    "capture_settings": wire.settings,
                    "max_scope_points": wire.max_length,
                },
                indent=2,
            ),
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
