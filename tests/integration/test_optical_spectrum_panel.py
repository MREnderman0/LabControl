from __future__ import annotations

import pytest

from lab_control.demo.workspace import create_demo_runtime
from lab_control.domain.instruments import DeviceError
from lab_control.ui.pages.monitoring.optical_spectrum import OpticalSpectrumMonitoringPage
from lab_control.ui.real_window import RealWindow


def shutdown(window):
    window.timer.stop()
    window.controller.close()
    window.runtime.close()
    window.closing = True
    window.bridge.worker.shutdown()


def test_readonly_optical_endpoint_is_cached_for_range_validation_without_an_editor(
    qtbot, tmp_path, monkeypatch
):
    from dataclasses import replace

    runtime = create_demo_runtime(tmp_path)
    window = RealWindow(runtime)
    qtbot.addWidget(window)
    declared = window.controller.declared_capabilities

    def partial(device, channel=1):
        schema = declared(device, channel)
        return replace(
            schema,
            operations=tuple(
                replace(op, writable=False) if op.name == "start_wavelength" else op
                for op in schema.operations
            ),
        )

    monkeypatch.setattr(window.controller, "declared_capabilities", partial)
    try:
        window.navigate("实时监测", "optical_spectrometer")
        page = window.stack.currentWidget()
        qtbot.waitUntil(lambda: len(page.controls.current()) == 4)
        assert set(page.controls.editors) == {"stop_wavelength", "rbw", "sensitivity"}
        assert "start_wavelength" not in page.form.editors
        original = page.controls.current()["start_wavelength"]
        page.controls.editors["rbw"].editors["value"].set_value(0.2)
        page.controls.apply()
        qtbot.waitUntil(lambda: not page.controls.running)
        assert (
            page.controls.current()["rbw"] == 0.2 and page.controls.current()["start_wavelength"] == original
        )
    finally:
        shutdown(window)


@pytest.mark.parametrize("layout_mode", ["electronic", "optical"])
def test_optical_page_has_four_instrument_settings_and_fixed_interval(
    qtbot, tmp_path, monkeypatch, layout_mode
):
    runtime = create_demo_runtime(tmp_path)
    window = RealWindow(runtime)
    qtbot.addWidget(window)
    window.show()
    try:
        window.controller.set_mode(layout_mode)
        window.navigate("实时监测", "optical_spectrometer")
        page = window.stack.currentWidget()
        assert isinstance(page, OpticalSpectrumMonitoringPage)
        assert set(page.controls.editors) == {"start_wavelength", "stop_wavelength", "rbw", "sensitivity"}
        assert page.timing.minimum() == 0
        assert page.timing.value() == 10.0
        assert page.selector.device_id
        assert window.controller.mode == "general"
        window.controller.ensure_applicable(page.device_id)
        with pytest.raises(DeviceError):
            window.controller.ensure_applicable(page.device_id, workspace_mode="invalid")
        page.timing.setValue(2.5)
        page.timing.interpretText()
        assert runtime.monitoring_preferences[page.timing.key]["interval"] == 2.5
        assert runtime.repository.load_workspace(page.timing.key)["interval"] == 2.5
        qtbot.waitUntil(lambda: len(page.controls.current()) == 4, timeout=3000)
        assert page.mode == page.controls.mode == page.timing.mode == "general"
        assert all(op.key.startswith("draft/general/") for op in page.controls.editors.values())
        page.controls.editors["rbw"].editors["value"].set_value(0.2)
        page.controls.apply()
        qtbot.waitUntil(lambda: not page.controls.running, timeout=3000)
        assert page.controls.current()["rbw"] == 0.2

        page.toggle_monitoring()
        task = runtime.monitoring.tasks[page.device_id]
        assert task.requests[0].initiate is True and task.requests[0].confirmed is True
        assert task.mode == "general"
        qtbot.waitUntil(
            lambda: runtime.monitoring.latest(page.device_id, "spectrum") is not None, timeout=3000
        )
        page.refresh_frame(page.device_id)
        assert page.plot.plot.getAxis("bottom").labelText == "波长"
        assert page.plot.plot.getAxis("left").labelText.startswith("光谱曲线")
        # Changing the global layout must not replace optical controls or move
        # their draft/timing preferences or interrupt the current monitor.
        controls = page.controls
        window.controller.set_mode("optical" if layout_mode == "electronic" else "electronic")
        page.refresh()
        assert page.controls is controls
        assert page.timing.key == f"monitor-timing/general/{page.device_id}"
        assert task.running and task.mode == "general"
        runtime.monitoring.stop(page.device_id).result(3)
    finally:
        shutdown(window)


def test_pending_optical_sync_and_failed_refresh_are_visible(qtbot, tmp_path, monkeypatch):
    runtime = create_demo_runtime(tmp_path)
    window = RealWindow(runtime)
    qtbot.addWidget(window)
    window.show()
    try:
        window.navigate("仪器测控", "optical_spectrometer")
        controls = window.stack.currentWidget().controls
        qtbot.waitUntil(lambda: len(controls.current()) == 4 and not controls.running)
        monkeypatch.setattr(runtime.monitoring, "settings_status", lambda *_: ("待同步", "等待有效读回"))
        controls.refresh()
        assert controls.health.text() == "待同步"
        from concurrent.futures import Future

        failed = Future()
        failed.set_exception(DeviceError("模拟读取超时"))
        submit = runtime.sessions.submit
        monkeypatch.setattr(
            runtime.sessions,
            "submit",
            lambda key, op, *args, **kw: failed if op == "read_settings" else submit(key, op, *args, **kw),
        )
        controls.refresh_button.click()
        qtbot.waitUntil(lambda: not controls.running and "模拟读取超时" in controls.status.text())
        assert controls.status.isVisible()
        partial = Future()
        partial.set_result(())
        monkeypatch.setattr(
            runtime.sessions,
            "submit",
            lambda key, op, *args, **kw: partial if op == "read_settings" else submit(key, op, *args, **kw),
        )
        controls.refresh_button.click()
        qtbot.waitUntil(lambda: not controls.running and "读回不完整" in controls.status.text())
        assert controls.status.isVisible()
    finally:
        shutdown(window)


def test_overview_and_optical_page_share_device_interval(qtbot, tmp_path):
    runtime = create_demo_runtime(tmp_path)
    window = RealWindow(runtime)
    qtbot.addWidget(window)
    window.controller.timer.stop()
    try:
        optical_id = next(
            record["id"]
            for record in runtime.inventory.all()
            if record.get("adapterId") == "sim.optical_spectrometer"
        )
        window.navigate("实时监测")
        card = window.dashboard.cards[optical_id]
        assert card.interval.value() == 10.0

        window.navigate("实时监测", "optical_spectrometer", optical_id)
        page = window.stack.currentWidget()
        assert isinstance(page, OpticalSpectrumMonitoringPage)
        page.timing.setValue(4.0)
        assert card.interval.value() == 4.0
        assert runtime.repository.load_workspace(f"monitor-timing/general/{optical_id}")["interval"] == 4.0

        window.navigate("实时监测")
        card.interval.setValue(7.5)
        assert page.timing.value() == 7.5
        assert runtime.repository.load_workspace(f"monitor-timing/general/{optical_id}")["interval"] == 7.5
    finally:
        shutdown(window)
