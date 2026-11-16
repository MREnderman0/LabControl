from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QWidget

from lab_control.application.acquisition_pipeline import SPECTRUM_INTERVAL
from lab_control.application.catalog import instrument_category
from lab_control.application.config import ConnectionConfig
from lab_control.application.runtime import ApplicationRuntime
from lab_control.instruments.registry import DEVICE_DEFINITIONS
from lab_control.ui.controller import WorkspaceController
from lab_control.ui.pages.monitoring.dashboard import MonitoringDashboard
from lab_control.ui.pages.monitoring.timing import MonitoringTiming, SpectrumInterval, monitoring_options

POLLING_ADAPTERS = sorted(
    key
    for key, definition in DEVICE_DEFINITIONS.items()
    if definition.monitoring_surface and definition.category != "phase_noise_analyzer"
) + ["sim.optical_spectrometer"]


@pytest.mark.parametrize("adapter", POLLING_ADAPTERS)
@pytest.mark.parametrize("cadence", ["interval", "fastest"])
def test_device_timing_survives_restarts_and_stays_isolated(tmp_path, qtbot, adapter, cadence):
    expected = {}
    for launch in range(3):
        with ApplicationRuntime(root=tmp_path, hardware=False) as runtime:
            keys = [
                runtime.add(ConnectionConfig(f"mock://startup/{index}", adapter, "mock"))
                for index in range(2)
            ]
            parent = QWidget()
            qtbot.addWidget(parent)
            controller = WorkspaceController(runtime, parent)
            controller.timer.stop()
            category = instrument_category(controller.records[keys[0]])
            default_interval = (
                1 if category == "power_supply" else 10 if category == "optical_spectrometer" else 0.5
            )
            default_cadence = (
                "interval"
                if category in {"power_supply", "oscilloscope", "spectrum_analyzer", "optical_spectrometer"}
                else "fastest"
            )
            if category == "spectrum_analyzer":
                default_cadence = SPECTRUM_INTERVAL
                widget = SpectrumInterval(controller, parent)
            else:
                widget = MonitoringTiming(controller, parent)
            for index, key in enumerate(keys):
                widget.bind(key, "general")
                interval, saved_cadence = expected.get(key, (default_interval, default_cadence))
                assert widget.options()["interval"] == interval
                assert widget.options()["cadence"] == saved_cadence
                # An unchanged final launch must not replace the saved choice.
                if launch < 2:
                    interval = (5, 10, 60)[index + launch]
                    saved_cadence = "interval" if category == "power_supply" else cadence
                    if category == "spectrum_analyzer":
                        saved_cadence = SPECTRUM_INTERVAL
                        if cadence == "fastest":
                            interval = (0, 0.01, 0.02)[index + launch]
                        widget.setValue(interval)
                    elif category == "power_supply":
                        widget.presets.setCurrentIndex(widget.presets.findData(interval))
                    else:
                        widget.interval.setValue(interval)
                        widget.cadence.setCurrentIndex(widget.cadence.findData(saved_cadence))
                    if category != "spectrum_analyzer":
                        widget.commit()
                    expected[key] = interval, saved_cadence
                saved = runtime.repository.load_workspace(f"monitor-timing/general/{key}")
                assert (saved["interval"], saved["cadence"]) == expected[key]
                assert monitoring_options(controller, key, "optical")["interval"] == default_interval
            widget.bind("", "general")
            widget.bind(keys[0], "general")
            assert widget.options()["interval"] == expected[keys[0]][0]
            assert not runtime.sessions.sessions and not runtime.monitoring.tasks
            controller.close()


@pytest.mark.parametrize(
    "adapter,default", [("rigol.dho924", 0.5), ("gwinstek.gpd4303s", 1), ("ceyear.6362d", 10)]
)
@pytest.mark.parametrize("saved", [None, {"interval": "bad"}, {"interval": 999}, {"cadence": "bad"}])
def test_invalid_saved_timing_uses_category_defaults(tmp_path, qtbot, adapter, default, saved):
    with ApplicationRuntime(root=tmp_path, hardware=False) as runtime:
        key = runtime.add(ConnectionConfig("mock://invalid-timing", adapter, "mock"))
        pref = f"monitor-timing/general/{key}"
        runtime.repository.save_workspace(pref, saved)
        controller = SimpleNamespace(runtime=runtime, records={r["id"]: r for r in runtime.inventory.all()})
        widget = MonitoringTiming(controller)
        qtbot.addWidget(widget)
        widget.bind(key, "general")
        assert widget.options()["interval"] == default
        assert widget.options()["cadence"] == "interval"
        assert runtime.repository.load_workspace(pref) == saved
        assert not runtime.sessions.sessions and not runtime.monitoring.tasks


@pytest.mark.parametrize(
    "adapter",
    [
        "lecroy.9404m",
        "rigol.dsa815",
        "gwinstek.gpd4303s",
        "joinwit.jw8105",
        "ceyear.6362d",
        "kplusk.fxe",
        "keysight.53220a",
        "rigol.m300",
    ],
)
@pytest.mark.parametrize("device_preference", [None, 10])
def test_overview_restores_device_timing_before_legacy_card_value(
    tmp_path, qtbot, adapter, device_preference
):
    with ApplicationRuntime(root=tmp_path, hardware=False) as runtime:
        key = runtime.add(ConnectionConfig("mock://overview-timing", adapter, "mock"))
        runtime.repository.save_workspace(
            "monitor_overview/all", {f"device:{key}": {"enabled": False, "interval": 5}}
        )
        if device_preference is not None:
            runtime.repository.save_workspace(
                f"monitor-timing/general/{key}", {"interval": device_preference, "cadence": "interval"}
            )
    expected = device_preference or 5
    for _ in range(2):
        with ApplicationRuntime(root=tmp_path, hardware=False) as runtime:
            runtime.connect(key).result(3)
            parent = QWidget()
            qtbot.addWidget(parent)
            controller = WorkspaceController(runtime, parent)
            controller.timer.stop()
            runtime.monitoring.enable_settings_sync(False)
            try:
                dashboard = MonitoringDashboard(controller, parent=parent)
                is_spectrum = instrument_category(controller.records[key]) == "spectrum_analyzer"
                timing = (
                    SpectrumInterval(controller, parent)
                    if is_spectrum
                    else MonitoringTiming(controller, parent)
                )
                timing.bind(key, "general")
                card = dashboard.cards[key]
                assert card.interval.value() == timing.options()["interval"] == expected
                assert timing.options()["cadence"] == (SPECTRUM_INTERVAL if is_spectrum else "interval")
                assert not card.enabled.isChecked()
                # Both editors must write the same persistent device preference.
                card.interval.setValue(60)
                assert timing.options()["interval"] == 60
                if is_spectrum:
                    timing.setValue(10)
                elif timing.power:
                    timing.presets.setCurrentIndex(timing.presets.findData(10))
                else:
                    timing.interval.setValue(10)
                assert card.interval.value() == 10
                assert runtime.repository.load_workspace(f"monitor-timing/general/{key}")["interval"] == 10
                assert not runtime.monitoring.tasks
                expected = 10
            finally:
                controller.close()
