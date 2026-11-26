"""Shared cadence controls: recording saves every acquired frame."""

import math

from PySide6.QtCore import Qt
from PySide6.QtGui import QValidator
from PySide6.QtWidgets import QDoubleSpinBox, QHBoxLayout, QLabel, QWidget

from lab_control.application.acquisition_pipeline import validate_cadence
from lab_control.application.catalog import instrument_category
from lab_control.domain.instruments import ValidationError
from lab_control.ui.widgets.workspace_combo import WorkspaceComboBox


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


class MonitoringTiming(QWidget):
    def __init__(self, controller, parent=None) -> None:
        super().__init__(parent)
        self.controller = controller
        self.device_id, self.mode = "", ""
        self.power = False
        self.fixed_interval = False
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        self.cadence = WorkspaceComboBox(self)
        self.cadence.addItem("最快采集", "fastest")
        self.cadence.addItem("自定义间隔", "interval")
        self.cadence.setToolTip("最快：上一帧收完即安排下一帧。开启记录后，每个有效采集帧都保存。")
        label = QLabel("采集节奏", self)
        self.cadence_label = label
        label.setObjectName("mutedText")
        label.setBuddy(self.cadence)
        layout.addWidget(label)
        layout.addWidget(self.cadence)
        self.cadence.currentIndexChanged.connect(self.change_cadence)
        self.interval = MonitoringIntervalSpinBox(self)
        self.interval.setSingleStep(0.05)
        self.interval.setValue(0.5)
        self.interval.setSuffix(" s")
        self.interval.setMinimumWidth(100)
        self.interval.setMaximumWidth(132)
        self.interval.setToolTip("相邻采集请求的目标开始间隔，范围 0～60 秒；不改变仪器扫描时间。")
        # Keyboard tracking is disabled: typing keeps a draft until focus
        # leaves the field or Enter is pressed. Arrows still apply immediately.
        self.interval.valueChanged.connect(self._interval_value_changed)
        label = QLabel("采集间隔", self)
        self.interval_label = label
        label.setObjectName("mutedText")
        label.setBuddy(self.interval)
        layout.addWidget(label)
        layout.addWidget(self.interval)
        self.presets = WorkspaceComboBox(self)
        for seconds in (1, 5, 10, 60):
            self.presets.addItem(f"{seconds} s", seconds)
        self.presets.setFixedWidth(90)
        self.presets.setToolTip("相邻采集请求的目标开始间隔；记录时逐帧保存，慢设备不补发。")
        self.presets.currentIndexChanged.connect(self._apply_preset)
        self.presets.hide()
        layout.addWidget(self.presets)
        layout.addStretch(1)
        self.interval.setEnabled(False)
        self.actual = QLabel("实际 — s/帧", self)
        self.actual.setObjectName("rangeHint")
        layout.addWidget(self.actual)
        if hasattr(controller, "monitoring_interval_changed"):
            controller.monitoring_interval_changed.connect(self._external_interval_changed)

    def _external_interval_changed(self, device_id: str, interval: float) -> None:
        if device_id != self.device_id:
            return
        if not self.interval.hasFocus():
            self.interval.blockSignals(True)
            self.interval.setValue(float(interval))
            self.interval.blockSignals(False)
        self.presets.blockSignals(True)
        self.presets.setCurrentIndex(self.presets.findData(float(interval)))
        self.presets.blockSignals(False)
        options = monitoring_options(self.controller, device_id, self.mode)
        self.cadence.blockSignals(True)
        self.cadence.setCurrentIndex(self.cadence.findData(options["cadence"]))
        self.cadence.blockSignals(False)
        self.interval.setEnabled(self.fixed_interval or options["cadence"] == "interval")

    def _apply_preset(self, index: int) -> None:
        if index >= 0:
            self.interval.setValue(self.presets.itemData(index))

    def _interval_value_changed(self, _value: float) -> None:
        if self.device_id:
            self.commit()

    def set_fixed_interval(self, fixed: bool = True) -> None:
        """Use the shared 0–60 second interval control without a cadence selector."""
        self.fixed_interval = fixed
        self._update_visibility()

    def _update_visibility(self) -> None:
        self.cadence_label.setVisible(not self.power and not self.fixed_interval)
        self.cadence.setVisible(not self.power and not self.fixed_interval)
        self.interval_label.show()
        self.interval.show()
        self.presets.setVisible(self.power and not self.fixed_interval)
        self.interval.setEnabled(self.fixed_interval or self.cadence.currentData() == "interval")

    def options(self) -> dict:
        interval = self.interval.value() if self.cadence.currentData() != "fastest" else 0.0
        return dict(interval=interval, record_interval=None, cadence="interval", csv_interval=5)

    def change_cadence(self) -> None:
        self.interval.setEnabled(self.fixed_interval or self.cadence.currentData() == "interval")
        self.commit()

    @property
    def key(self) -> str:
        return f"monitor-timing/{self.mode}/{self.device_id}"

    def bind(self, device_id: str, mode: str) -> None:
        if (device_id, mode) == (self.device_id, self.mode):
            return
        self.device_id, self.mode = device_id, mode
        self.power = instrument_category(self.controller.records.get(device_id, {})) == "power_supply"
        self._update_visibility()
        self.setEnabled(bool(device_id))
        options = monitoring_options(self.controller, device_id, mode)
        self.cadence.blockSignals(True)
        self.cadence.setCurrentIndex(self.cadence.findData(options["cadence"]))
        self.cadence.blockSignals(False)
        self.interval.setEnabled(self.fixed_interval or options["cadence"] == "interval")
        self.interval.blockSignals(True)
        self.interval.setValue(options["interval"])
        self.interval.blockSignals(False)
        self.presets.blockSignals(True)
        self.presets.setCurrentIndex(self.presets.findData(options["interval"]))
        self.presets.blockSignals(False)
        self.refresh(self.controller.runtime.monitoring.snapshots().get(device_id, {}))

    def commit(self) -> None:
        if not self.device_id:
            return
        options = self.options()
        try:
            save_monitoring_interval(
                self.controller,
                self.device_id,
                self.mode,
                options["interval"],
                cadence=options["cadence"],
            )
        except Exception as exc:
            self.controller.message.emit(str(exc))

    def refresh(self, task: dict) -> None:
        if (
            self.device_id
            and not self.interval.hasFocus()
            and not self.cadence.view().isVisible()
            and not self.presets.view().isVisible()
        ):
            options = monitoring_options(self.controller, self.device_id, self.mode)
            self.cadence.blockSignals(True)
            self.interval.blockSignals(True)
            self.cadence.setCurrentIndex(self.cadence.findData(options["cadence"]))
            self.interval.setValue(options["interval"])
            self.interval.setEnabled(self.fixed_interval or options["cadence"] == "interval")
            self.presets.blockSignals(True)
            self.presets.setCurrentIndex(self.presets.findData(options["interval"]))
            self.presets.blockSignals(False)
            self.cadence.blockSignals(False)
            self.interval.blockSignals(False)
        actual = task.get("frame_interval")
        self.actual.setText(f"实际 {actual:.2f} s/帧" if actual is not None else "实际 — s/帧")
