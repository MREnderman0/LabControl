from contextlib import contextmanager

from lab_control.application.config import ConnectionConfig
from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.processing import RecordedDevice, RecordingEntry
from lab_control.instruments.registry import DEVICE_DEFINITIONS
from lab_control.ui.real_window import RealWindow


@contextmanager
def simulated_window(qtbot, tmp_path, models):
    with ApplicationRuntime(root=tmp_path, hardware=False) as runtime:
        devices = {}
        for model in models:
            device = runtime.add(ConnectionConfig(f"mock://unified/{model}", model, "mock"))
            runtime.connect(device).result(5)
            devices[model] = device
        runtime.repository.save_workspace(
            "monitor_overview/all", {f"device:{device}": {"enabled": True} for device in devices.values()}
        )
        window = RealWindow(runtime)
        qtbot.addWidget(window)
        window.show()
        try:
            yield window, runtime, devices
        finally:
            window.timer.stop()
            window.controller.close()
            window.closing = True
            window.close()
            window.bridge.worker.shutdown()


def test_signal_channel_writes_and_legacy_batch_preferences(qtbot, tmp_path, monkeypatch):
    sources = [
        key for key, definition in DEVICE_DEFINITIONS.items() if definition.category == "signal_generator"
    ]
    with simulated_window(qtbot, tmp_path, [*sources, "joinwit.jw8105"]) as (window, runtime, devices):
        for model in sources:
            device = devices[model]
            adapter = runtime.sessions.sessions[device].adapter
            writes = []
            apply = adapter.apply
            monkeypatch.setattr(
                adapter, "apply", lambda cmd, original=apply, log=writes: log.append(cmd) or original(cmd)
            )
            window.navigate("实时监测", "signal_generator", device)
            page = window.stack.currentWidget()
            assert page.selector.device_id == device
            if 2 in page.panels:
                page.selector.select(device, 2)
                for ch in (1, 2):
                    group = page.panels[ch]
                    editor = group.editors["offset"]
                    qtbot.waitUntil(lambda: editor.current_value is not None)
                    field = editor.editors["value"].input
                    field.setFocus()
                    field.selectAll()
                    qtbot.keyClicks(field, str(ch / 10))
                    qtbot.waitUntil(group.apply_button.isEnabled)
                    assert len(writes) == ch - 1
                    group.apply_button.click()
                    qtbot.waitUntil(lambda: not group.running)
                runtime.monitoring.refresh_settings(device)
                for ch in (1, 2):
                    editor = page.panels[ch].editors["offset"]
                    qtbot.waitUntil(lambda: editor.current_value == {"value": ch / 10})
                assert [(cmd.channel, cmd.operation, cmd.parameters) for cmd in writes] == [
                    (ch, "offset", {"value": ch / 10}) for ch in (1, 2)
                ]
            else:
                assert not writes
        window.navigate("实时监测")
        meter = devices["joinwit.jw8105"]
        assert set(window.dashboard.cards) == {meter}
        window.dashboard.sample_button.click()
        qtbot.waitUntil(lambda: runtime.monitoring.snapshots().get(meter, {}).get("recording", False))
        assert set(runtime.monitoring.tasks) == {meter}
        runtime.monitoring.stop(meter).result(5)


def test_scope_card_focus_depth_and_pico_frame(qtbot, tmp_path, monkeypatch):
    with simulated_window(qtbot, tmp_path, ["rigol.ds1202ze", "pico.6404e"]) as (window, runtime, devices):
        device = devices["rigol.ds1202ze"]
        adapter = runtime.sessions.sessions[device].adapter
        writes = []
        apply = adapter.apply
        monkeypatch.setattr(adapter, "apply", lambda cmd: writes.append(cmd) or apply(cmd))
        window.dashboard.add(device, "waveform", (2,))
        window.dashboard.refresh()
        window.dashboard._open_device(device)
        page = window.stack.currentWidget()
        controls = page.controls
        assert controls.focused_channel == 2
        assert controls.pages.currentWidget().layout() is controls.tab_pages["垂直"]
        qtbot.waitUntil(lambda: page.readouts.cells["points"].value.text() == "AUTO")
        assert not writes
        controls.show_channel(1)
        window.navigate("数据处理")
        history = window.stack.currentWidget()
        recording = RecordedDevice(tmp_path, {"device_id": device})
        history.open_entry(RecordingEntry(tmp_path, "single_device", "", (recording,)))
        history.viewer.info = {
            "series": [{"quantity": "waveform", "unit": "V", "axis_unit": "s", "channel": 2, "id": "CH2"}]
        }
        history.viewer.group.addItem("波形", ("waveform", "V", "s"))
        history.open_device_button.click()
        assert window.stack.currentWidget() is page and page.device_id == device
        assert controls.focused_channel == 2 and not writes
        controls.set_points("12000")
        qtbot.waitUntil(lambda: page.readouts.cells["points"].value.text() == "12,000")
        assert [(cmd.operation, cmd.parameters) for cmd in writes] == [("record_length", {"value": "12000"})]
        assert page.readouts.cells["actual_points"].value.text() == "—"
        window.navigate("仪器测控", "oscilloscope", devices["pico.6404e"])
        for mode in ("waveform", "streaming"):
            page.controls.capture_mode.setCurrentIndex(page.controls.capture_mode.findData(mode))
            page.start()
            qtbot.waitUntil(
                lambda: bool(page.plot.displayed_frame and page.plot.displayed_frame.request.kind == mode)
            )
            frame = page.plot.displayed_frame
            assert frame.batch.configuration["sample_interval_s"] > 0
            assert page.readouts.cells["rate"].value.text() != "—"
            assert (
                page.readouts.cells["actual_points"].value.text()
                == f"{len(frame.batch.measurements[0].values):,}"
            )
            runtime.monitoring.stop(devices["pico.6404e"]).result(5)
            window.controller.poll()
