from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QWidget

from lab_control.application.catalog import instrument_category
from lab_control.application.config import ConnectionConfig
from lab_control.application.runtime import ApplicationRuntime
from lab_control.instruments.registry import DEVICE_DEFINITIONS
from lab_control.ui.controller import WorkspaceController
from lab_control.ui.pages.monitoring.dashboard import MonitoringDashboard
from lab_control.ui.pages.monitoring.timing import MonitoringInterval, monitoring_options

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
            default_interval = {
                "power_supply": 1,
                "optical_spectrometer": 10,
                "optical_power_meter": 0.5,
                "spectrum_analyzer": 0.5,
                "oscilloscope": 0.5,
            }.get(category, 0)
            default_cadence = "interval"
            widget = MonitoringInterval(controller, parent)
            for index, key in enumerate(keys):
                widget.bind(key, "general")
                interval, saved_cadence = expected.get(key, (default_interval, default_cadence))
                assert widget.options()["interval"] == interval
                assert widget.options()["cadence"] == saved_cadence
                # An unchanged final launch must not replace the saved choice.
                if launch < 2:
                    interval = (5, 7.5, 60)[index + launch]
                    saved_cadence = "interval"
                    if cadence == "fastest" and launch == 0:
                        preference_key = f"monitor-timing/general/{key}"
                        runtime.repository.save_workspace(
                            preference_key, {"interval": interval, "cadence": "fastest"}
                        )
                        runtime.monitoring_preferences.pop(preference_key, None)
                        widget.bind(key, "general")
                        assert widget.value() == 0
                    interval = (0.01, 0.50, 60)[index + launch] if cadence == "fastest" else interval
                    widget.setValue(interval)
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
        widget = MonitoringInterval(controller)
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
                timing = MonitoringInterval(controller, parent)
                timing.bind(key, "general")
                card = dashboard.cards[key]
                assert card.interval.value() == timing.options()["interval"] == expected
                assert timing.options()["cadence"] == "interval"
                assert not card.enabled.isChecked()
                # Both editors must write the same persistent device preference.
                card.interval.setValue(60)
                assert timing.options()["interval"] == 60
                timing.setValue(10)
                assert card.interval.value() == 10
                assert runtime.repository.load_workspace(f"monitor-timing/general/{key}")["interval"] == 10
                assert not runtime.monitoring.tasks
                expected = 10
            finally:
                controller.close()
