"""Exercise the real graphite UI against a simulated CH1 output-to-input cable."""

import json

import numpy as np
import pytest
from PySide6.QtCore import QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication, QMessageBox

from lab_control.app import load_stylesheet, load_ui_font
from lab_control.ui.pages.monitoring.spectrum_markers import MarkerDialog
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
        if controls.scope:
            controls.tabs.button(
                {"scope_timebase": 0, "scope_trigger": 2, "scope_acquisition": 3, "scope_interpolation": 3}[
                    group
                ]
            ).click()
        for key, val in values.items():
            editor = controls.editors[group][key]
            if editor.spec.kind == "enum":
                editor.input.setCurrentIndex(editor.input.findData(val))
            else:
                editor.set_value(val)
                editor.changed.emit()
        wait(controls.apply_button.isEnabled)
        before = controls.receipt.text()
        controls.apply_button.click()
        wait(lambda: controls.receipt.text() and controls.receipt.text() != before)
        return controls.receipt.text()

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
                scope.capture.tabs.button(1).click()
                bandwidth = scope.capture.front_fields["bandwidth"]
                bandwidth.setCurrentIndex(bandwidth.findData("600MHz"))
                with monkeypatch.context() as cancel:
                    cancel.setattr(QMessageBox, "question", lambda *_: QMessageBox.StandardButton.No)
                    count = len(wire.calls)
                    scope.capture.apply_button.click()
                    assert len(wire.calls) == count
                scope.capture.apply_button.click()
                wait(lambda: "600MHz" in scope.readback.text())
                assert "设置回执" in configure(
                    scope, "scope_trigger", mode="AUTO", edge="FALLING", level=0.01
                )
                assert wire.settings["trigger"]["source"] == "ChannelB"
                assert "Precision" in configure(scope, "scope_acquisition", mode="PRECISION")
                assert "Linear" in configure(scope, "scope_interpolation", interpolation="LINEAR")
                assert "设备读回" in configure(
                    scope, "scope_timebase", seconds_per_div=40e-6, center=0, max_length=512
                )
                scope.monitor_button.click()
                wait(lambda: scope.last and "Precision / Linear" in scope.settings.text())
                measurement = scope.last.batch.measurements[0]
                np.testing.assert_allclose(measurement.axis[[0, -1]], [-0.0002, 0.0002])
                zero = np.argmin(abs(measurement.axis))
                assert measurement.values[zero] == pytest.approx(0.01, abs=15e-6)
                assert measurement.values[zero - 1] > measurement.values[zero + 1]
                assert scope.plot.grid_range == pytest.approx((-0.0002, 0.0002))
                assert scope.capture.editors["scope_timebase"]["seconds_per_div"].value() == pytest.approx(
                    40e-6
                )
                scope.capture.tabs.button(1).click()
                scope.capture.display.auto.setChecked(False)
                scope.capture.display.top.setValue(20)
                scope.capture.display.offset.setValue(5)
                assert scope.plot.display_specs[1].reference_value == pytest.approx(0.005)
                assert scope.plot.display_specs[1].units_per_div == pytest.approx(0.02)
                capture(scope, "scope-vertical")
                scope.capture.tabs.button(2).click()
                capture(scope, "scope-trigger")
                scope.capture.tabs.button(3).click()
                capture(scope, "scope-acquisition")
                scope.capture.tabs.button(0).click()
                capture(scope, "scope-settings")
                # A new frame must leave an unfinished edit intact.
                editor = scope.capture.editors["scope_timebase"]["t1"]
                editor.input.setText("-125")
                editor.input.textEdited.emit("-125")
                sequence = scope.last.sequence
                wait(lambda: scope.last.sequence > sequence)
                assert editor.input.text() == "-125"
                assert wire.settings["timebase"]["t1"] == -0.0002
                scope.capture.refresh_button.click()
                wait(lambda: scope.capture.receipt.text() == "参数已刷新")
                assert editor.input.text() == "-125"
                scope.stop_button.click()
                wait(lambda: not scope.stop_button.isEnabled())
                spectrum = open_role("spectrum_analyzer")
                assert "600 MHz" in configure(spectrum, "spectrum_span", frequency1=300e6, frequency2=600e6)
                assert wire.settings["span"]["frequency2"] == 600e6
                wideband_readback = wire.settings["span"].copy()
                assert "Hamming" in configure(spectrum, "spectrum_window", window="HAMMING")
                assert "设置回执" in configure(spectrum, "spectrum_averaging", target_duration=0.2)
                assert "设备读回" in configure(spectrum, "spectrum_span", center=15000, span=20000)
                attenuation = spectrum.capture.front_fields["attenuation"]
                attenuation.setCurrentIndex(attenuation.findData("20dB"))
                assert "2 组" in configure(spectrum, "spectrum_rbw", mode="MANUAL", rbw_value=275)
                assert wire.front[1]["attenuation"] == "20dB"
                assert "300 Hz" in spectrum.capture.receipt.toolTip()
                spectrum.monitor_button.click()
                wait(lambda: spectrum.last and "300 Hz" in spectrum.settings.text())
                assert "600MHz" in spectrum.readback.text()
                np.testing.assert_allclose(spectrum.last.batch.measurements[0].axis[[0, -1]], [5000, 25000])
                spectrum.capture.display.auto.setChecked(False)
                spectrum.capture.display.top.setValue(-10)

                def set_marker(frequency, tracking):
                    dialog = QApplication.activeModalWidget()
                    assert isinstance(dialog, MarkerDialog)
                    dialog.frequency.set_value(frequency)
                    dialog.tracking.setChecked(tracking)
                    dialog.search_width.set_value(2500)
                    dialog.save()

                QTimer.singleShot(0, lambda: set_marker(10000, True))
                spectrum.capture.add_marker.click()
                wait(lambda: spectrum.plot.readings.get(1) and spectrum.plot.readings[1].position == 10000)
                assert spectrum.plot.readings[1].amplitude == pytest.approx(-16.02, abs=0.05)
                QTimer.singleShot(0, lambda: set_marker(15000, False))
                spectrum.capture.add_marker.click()
                assert spectrum.plot.readings[2].position == 15000
                rows = spectrum.marker_list
                wait(
                    lambda: (
                        rows.viewport().rect().contains(rows.visualItemRect(rows.item(1)))
                        and rows.parentWidget().height() >= rows.y() + rows.height()
                    )
                )
                marker_readings = {
                    str(n): {"hz": r.position, "dbm": r.amplitude} for n, r in spectrum.plot.readings.items()
                }
                assert spectrum.plot.plot.viewRange()[1] == pytest.approx([-110, -10])
                capture(spectrum, "spectrum-settings")
                window.resize(1100, 700)
                qtbot.wait(60)
                assert spectrum.capture.scroll_area.horizontalScrollBar().maximum() == 0
                window.resize(1430, 900)
                spectrum.stop_button.click()
                wait(lambda: not spectrum.stop_button.isEnabled())
                scope = open_role("oscilloscope")
            scope.record_button.click()
            wait(lambda: scope.last and scope.record_button.text() == "停止记录")
            assert scope.chart.property("recordingPhase") == "recording"
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
            wait(lambda: spectrum.last and spectrum.record_button.text() == "停止记录")
            assert spectrum.chart.property("recordingPhase") == "recording"
            spectrum.refresh_status()
            assert "共用 ch1 输入前端" in spectrum.role_bar.note.text()
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
            scope.record_button.click()
            wait(lambda: scope.record_button.text() == "逐帧记录")
            shared = runtime.monitoring.snapshots()[device]
            assert set(shared["recording_kinds"]) == {"spectrum"}
            assert set(shared["kinds"]) == {"waveform", "spectrum"}
            scope.stop_button.click()
            wait(lambda: not scope.stop_button.isEnabled())
            spectrum = open_role("spectrum_analyzer")
            previous = spectrum.last.sequence
            wait(lambda: spectrum.last.sequence > previous)
            stop(spectrum)

        source = open_role("signal_generator")
        card.off.click()
        wait(lambda: wire.outputs[4]["type"] == "Off")
        wait(lambda: card.output.label.text() == "已关闭")
        assert source.role_bar.chips.group.button(0).text() == "信号源 · CH1 已关闭"
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
                    "software_markers_at_10khz": marker_readings,
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


@pytest.mark.parametrize("bad_reply", ["mismatch", "timeout"])
def test_ambiguous_output_write_replaces_only_its_channel_receipt(qtbot, tmp_path, monkeypatch, bad_reply):
    wire = LoopbackWire()
    runtime, device, _ = connected(tmp_path, wire)
    window = RealWindow(runtime)
    qtbot.addWidget(window)
    monkeypatch.setattr(QMessageBox, "question", lambda *_: QMessageBox.StandardButton.Yes)

    try:
        window.show()
        window.navigate("仪器测控", "signal_generator", device)
        page = window.stack.currentWidget()
        card = page.cards[1]
        qtbot.waitUntil(card.off.isEnabled)
        card.off.click()
        qtbot.waitUntil(lambda: card.output.label.text() == "已关闭")
        qtbot.waitUntil(page.cards[2].off.isEnabled)
        page.cards[2].off.click()
        qtbot.waitUntil(lambda: page.role_bar.chips.group.button(0).text() == "信号源 · CH1、CH2 已关闭")
        original = wire.call

        def bad_ack(method, path, params=None):
            result = original(method, path, params)
            if path.endswith("/generate_waveform"):
                if bad_reply == "timeout":
                    raise TimeoutError("模拟设备已执行，回执超时")
                return {}
            return result

        monkeypatch.setattr(wire, "call", bad_ack)
        qtbot.waitUntil(card.on.isEnabled)
        card.on.click()
        qtbot.waitUntil(lambda: card.output.label.text() == "状态待核实")
        assert wire.outputs[4]["type"] == "Sine"
        assert page.cards[2].output.label.text() == "已关闭"
        window.navigate("仪器测控", "oscilloscope", device)
        chip = window.stack.currentWidget().role_bar.chips.group.button(0)
        assert chip.text() == "信号源 · CH1 状态待核实 · CH2 已关闭"
        assert sum(p.endswith("/generate_waveform") for _, p, _ in wire.calls) == 3
        runtime.sessions.disconnect(device)
        runtime.connect(device).result(4)
        window.controller.poll()
        assert chip.text() == "信号源 · 无输出回执"
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
