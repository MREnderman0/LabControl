"""Shared cadence controls: recording saves every acquired frame."""

import math

from PySide6.QtCore import Qt
from PySide6.QtGui import QValidator
from PySide6.QtWidgets import QDoubleSpinBox

from lab_control.application.acquisition_pipeline import validate_cadence
from lab_control.application.catalog import instrument_category
from lab_control.domain.instruments import ValidationError


class MonitoringIntervalSpinBox(QDoubleSpinBox):
    """Keep keyboard drafts intact; apply bounds only when editing finishes."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setRange(0, 60)
        self.setDecimals(2)
        self.setKeyboardTracking(False)

    def validate(self, text: str, position: int):
        state, normalized, position = super().validate(text, position)
        if state == QValidator.State.Invalid:
            return QValidator.State.Intermediate, text, position
        return state, normalized, position

    def fixup(self, text: str) -> str:
        value_text = text.removeprefix(self.prefix()).removesuffix(self.suffix()).strip()
        value, valid = self.locale().toDouble(value_text)
        value = (
            min(self.maximum(), max(self.minimum(), value))
            if valid and math.isfinite(value)
            else self.value()
        )
        return self.prefix() + self.textFromValue(value) + self.suffix()


def monitoring_intervals(controller, device_id: str, mode: str) -> tuple[float, None]:
    options = monitoring_options(controller, device_id, mode)
    return options["interval"], None


def monitoring_options(controller, device_id: str, mode: str) -> dict:
    category = instrument_category(controller.records.get(device_id, {}))
    default_interval = {
        "optical_spectrometer": 10.0,
        "power_supply": 1.0,
        "spectrum_analyzer": 0.5,
        "oscilloscope": 0.5,
        "optical_power_meter": 0.5,
    }.get(category, 0.0)
    key = f"monitor-timing/{mode}/{device_id}"
    preferences = controller.runtime.monitoring_preferences
    if key not in preferences:
        preferences[key] = controller.runtime.repository.load_workspace(key, {}) if device_id else {}
    stored = preferences[key]
    task = controller.runtime.monitoring.snapshots().get(device_id, {})
    values = task if task.get("running") else stored
    try:
        interval = (
            0.0 if values.get("cadence") == "fastest" else float(values.get("interval", default_interval))
        )
        validate_cadence(interval, "interval")
    except (AttributeError, TypeError, ValueError, ValidationError):
        interval = default_interval
    # Old independent save/CSV preferences never throttle the new UI path.
    return dict(interval=interval, record_interval=None, cadence="interval", csv_interval=5)


def save_monitoring_interval(
    controller,
    device_id: str,
    mode: str,
    interval: float,
    *,
    cadence: str | None = None,
    notify: bool = True,
) -> float:
    """Persist one device's interval and update an active scheduler.

    The overview and device pages intentionally share this small write path.
    Updating an interval changes only local scheduling and preferences.
    """
    if not device_id:
        return float(interval)
    interval = float(interval)
    if cadence == "fastest":
        interval = 0.0
    options = dict(interval=interval, record_interval=None, cadence="interval", csv_interval=5)
    controller.runtime.monitoring.set_intervals(device_id, **options)
    key = f"monitor-timing/{mode}/{device_id}"
    controller.runtime.monitoring_preferences[key] = dict(options)
    controller.runtime.repository.save_workspace(key, options)
    if notify and hasattr(controller, "monitoring_interval_changed"):
        controller.monitoring_interval_changed.emit(device_id, interval)
    return interval


class MonitoringInterval(MonitoringIntervalSpinBox):
    """Single numeric control shared with the overview's per-device preference."""

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.device_id, self.mode = "", ""
        self.setSingleStep(0.01)
        self.setObjectName("spectrumInterval")
        self.setButtonSymbols(self.ButtonSymbols.NoButtons)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setPrefix("采集间隔 ")
        self.setSuffix(" s")
        self.setAccessibleName("采集间隔（秒）")
        self.setToolTip("采集间隔（秒）。0 表示收完即采；正数为相邻两帧的目标开始间隔。")
        self.valueChanged.connect(self._save)
        if hasattr(controller, "monitoring_interval_changed"):
            controller.monitoring_interval_changed.connect(self._external_change)
        self.setEnabled(False)

    @property
    def key(self):
        return f"monitor-timing/{self.mode}/{self.device_id}"

    def bind(self, device_id, mode):
        if (device_id, mode) == (self.device_id, self.mode) and self.hasFocus():
            return
        self.device_id, self.mode = device_id, mode
        self.setEnabled(bool(device_id))
        self._sync(device_id, self.options()["interval"])

    def options(self):
        return monitoring_options(self.controller, self.device_id, self.mode)

    def _external_change(self, device_id, value):
        if self.hasFocus() and self.lineEdit().isModified():
            return
        self._sync(device_id, value)

    def _sync(self, device_id, value):
        if device_id == self.device_id and value != self.value():
            blocked = self.blockSignals(True)
            self.setValue(value)
            self.blockSignals(blocked)

    def _save(self, value):
        if self.device_id:
            try:
                save_monitoring_interval(self.controller, self.device_id, self.mode, value)
            except Exception as exc:
                self._sync(self.device_id, self.options()["interval"])
                self.controller.message.emit(str(exc))

    def refresh(self, task):
        if self.device_id and not self.hasFocus():
            if task.get("running"):
                value = 0 if task.get("cadence") == "fastest" else task["interval"]
                self._sync(self.device_id, value)
            else:
                self._sync(self.device_id, self.options()["interval"])


def actual_interval_text(task: dict) -> str:
    actual = task.get("frame_interval")
    return f"实际 {actual:.2f} s/帧" if actual is not None else "实际 — s/帧"
