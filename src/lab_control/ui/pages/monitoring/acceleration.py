"""Continuous signed scalar acceleration, using the optical meter's monitoring workspace."""

from __future__ import annotations

import math
from datetime import datetime

from PySide6.QtWidgets import QCheckBox, QSizePolicy

from lab_control.domain.instruments import AcquisitionRequest
from lab_control.ui.pages.monitoring.optical_power import (
    OpticalPowerMonitoringPage,
    OpticalPowerPlot,
    PowerMeterReadoutCard,
)


class AccelerationPlot(OpticalPowerPlot):
    quantity = "acceleration"
    axis_label = "加速度"
    display_unit = "g"
    nonnegative = False

    def time_range(self, elapsed):
        if elapsed >= 600:
            return elapsed - 600, elapsed
        return 0.0, next(limit for limit in (5, 10, 20, 30, 60, 120, 300, 600) if limit >= elapsed)

    def display_value(self, value, unit):
        return value if unit == "g" else None

    def show_frame(self, frame, channels):
        present = {m.channel for m in frame.batch.measurements if m.quantity == self.quantity}
        valid = tuple(channel for channel in channels if channel in present)
        if not valid or not super().show_frame(frame, valid):
            return False
        self.mark_gap(set(channels) - present)
        self.set_channels(channels)
        return True

    def mark_gap(self, channels):
        for channel in channels:
            history = self.history.get(channel)
            if history and math.isfinite(history[-1][1]):
                history.append((history[-1][0], math.nan))


class AccelerationCard(PowerMeterReadoutCard):
    display_unit = "g"
    HEIGHT = 94

    def __init__(self, channel, parent=None):
        super().__init__(channel, parent)
        self.setMinimumWidth(186)
        self.setMaximumWidth(16777215)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.channel_label.hide()
        self.check = QCheckBox(f"CH {channel}", self)
        self.heading.insertWidget(0, self.check)

    def display_value(self, value, unit):
        return value if unit == "g" else None

    def unavailable(self, reason):
        self.update_measurement(None)
        self.status.setText(reason)
        self.status.setStyleSheet(
            "color: #ea9b82;" if reason in {"IEPE 开路", "IEPE 过流", "超量程", "输入异常"} else ""
        )
        self.meta.setText("")

    def update_measurement(self, measurement, timestamp=None):
        super().update_measurement(measurement, timestamp)
        self.status.setStyleSheet("")
        if measurement is not None and isinstance(timestamp, str):
            local = datetime.fromisoformat(timestamp).astimezone()
            self.meta.setText("更新时间  " + local.strftime("%H:%M:%S.%f")[:-3])


class AccelerationMonitoringPage(OpticalPowerMonitoringPage):
    category = "accelerometer"
    category_name = "加速度采集卡"
    quantity = "acceleration"
    trend_title = "瞬时加速度 · 最近 10 min"
    rolling_power = False
    plot_type = AccelerationPlot
    card_type = AccelerationCard

    def __init__(self, controller, add_card, parent=None):
        super().__init__(controller, add_card, parent)
        self.channel_bar.hide()
        self.splitter.setSizes([800, 280])

    def request(self, channels, *, confirmed=False):
        return AcquisitionRequest("scalar", channels, 1, self.quantity, initiate=True, confirmed=confirmed)

    def _reflow_readout_cards(self):
        while self.readout_grid.count():
            self.readout_grid.takeAt(0)
        for index, card in enumerate(self.cards.values()):
            self.readout_grid.addWidget(card, index, 0)
            card.show()

    def refresh_channel_names(self):
        super().refresh_channel_names()
        details = self.controller.states.get(self.device_id, {}).get("capabilityDetails", {})
        physical = details.get("physical_inputs", {})
        names = self.controller.channel_names(self.device_id) if self.device_id else {}
        legend_names = {}
        for channel, check in self.channel_checks.items():
            label = names.get(str(channel)) or f"CH {channel}"
            pin = physical.get(str(channel), "")
            title = f"{label} · {pin}" if pin else label
            check.setText(title.replace("&", "&&"))
            check.setAccessibleName(title)
            check.setToolTip("选择监测及记录的输入通道")
            legend_names[str(channel)] = names.get(str(channel)) or pin or label
        self.plot.names = legend_names
        self.plot.refresh_legend()

    def refresh_frame(self, device_id):
        super().refresh_frame(device_id)
        frame = self.latest_frame
        if not frame or device_id != self.device_id:
            return
        states = frame.batch.configuration.get("channel_status", {})
        physical = (
            self.controller.states.get(device_id, {}).get("capabilityDetails", {}).get("physical_inputs", {})
        )
        for channel, card in self.cards.items():
            name = physical.get(str(channel), "")
            state = states.get(name.lower(), "")
            if self.channel_checks[channel].isChecked() and state and state != "正常":
                card.unavailable(state)

    def refresh(self):
        previous = self.monitoring_context_id
        super().refresh()
        task = self.controller.runtime.monitoring.snapshots().get(self.device_id, {})
        busy = bool(task.get("running") or task.get("pending") or task.get("stopping"))
        # Keep the full recording path accessible without letting it displace the trend.
        status_text = self.status.text()
        self.status.setToolTip(status_text)
        if "\n" in status_text:
            self.status.setText(status_text.splitlines()[0])
        waiting = (
            busy
            and task.get("notice")
            and not self.controller.runtime.monitoring.latest(self.device_id, "scalar", self.quantity)
        )
        if waiting:
            self.plot.mark_gap(self.channels())
        for channel, card in self.cards.items():
            check = self.channel_checks[channel]
            if busy:
                check.setEnabled(False)
            if not check.isChecked():
                card.unavailable("未选择")
            elif waiting:
                card.unavailable("输入稳定中" if "稳定" in task["notice"] else "等待有效输入")
            elif self.latest_frame is None and (busy or previous != self.monitoring_context_id):
                card.unavailable("等待输入稳定" if busy else "等待数据")
            elif not busy and self.latest_frame:
                if card.status.text() == "正常":
                    card.status.setText("最后读数")
        if (
            not busy
            and task.get("sequence")
            and not (task.get("error") or task.get("notice") or task.get("recording_notice"))
        ):
            self.status.setText("监测已停止 · 已保留最后读数")
