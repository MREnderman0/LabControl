"""Compact capability-driven scope sliders; one serial control group per gesture."""

from __future__ import annotations

import math
import re
from typing import Any

from PySide6.QtCore import QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QStackedWidget,
    QStyle,
    QStyleOptionSlider,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from lab_control.domain.instruments import DeviceCommand, DeviceError, Parameter
from lab_control.domain.oscilloscope import COLORS, CONTROL_NAMES, coarse_steps, engineering
from lab_control.domain.readback import boolean_value, normalized_value, usable_reading
from lab_control.ui.kit import Dot, Pill, Segmented, Switch, set_property, style_button
from lab_control.ui.kit.numeric import ReadingLabel
from lab_control.ui.pages.monitoring.frame import show_sync_health
from lab_control.ui.pages.monitoring.scope_depth import ScopeDepth
from lab_control.ui.pages.monitoring.scope_points import ScopePoints
from lab_control.ui.styles import tokens as tk
from lab_control.ui.widgets.workspace_combo import WorkspaceComboBox


def gear_icon() -> QIcon:
    pixmap = QPixmap(48, 48)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.scale(2, 2)
    painter.translate(12, 12)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(tk.TEXT_2))
    for _ in range(8):
        painter.drawRoundedRect(QRectF(-1.8, -9, 3.6, 4), 0.7, 0.7)
        painter.rotate(45)
    painter.drawEllipse(QRectF(-6.5, -6.5, 13, 13))
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationOut)
    painter.drawEllipse(QRectF(-3, -3, 6, 6))
    painter.end()
    return QIcon(pixmap)


class GestureSlider(QSlider):
    key_started = Signal()
    mouse_started = Signal()
    mouse_finished = Signal()
    key_finished = Signal()
    gesture_cancelled = Signal()
    fine_step = Signal(int)
    fine_moved = Signal(float)
    fine_drag = False
    drag_origin: float | None = None
    groove_drag = False
    linear = False
    key_active = False
    mouse_active = False

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            self.gesture_cancelled.emit()
            event.accept()
            return
        self.key_active = True
        self.key_started.emit()
        directions = {
            Qt.Key.Key_Left: -1,
            Qt.Key.Key_Down: -1,
            Qt.Key.Key_Right: 1,
            Qt.Key.Key_Up: 1,
            Qt.Key.Key_PageDown: -10,
            Qt.Key.Key_PageUp: 10,
        }
        if self.linear and event.key() in directions:
            self.fine_step.emit(directions[event.key()])
            event.accept()
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event) -> None:  # noqa: N802
        super().keyReleaseEvent(event)
        if not event.isAutoRepeat():
            self.key_active = False
            self.key_finished.emit()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.mouse_active = True
            self.mouse_started.emit()
        if event.button() == Qt.MouseButton.LeftButton and not self.fine_drag:
            option = QStyleOptionSlider()
            self.initStyleOption(option)
            handle = self.style().subControlRect(
                QStyle.ComplexControl.CC_Slider, option, QStyle.SubControl.SC_SliderHandle, self
            )
            if not handle.contains(event.position().toPoint()):
                self.groove_drag = True
                self.setSliderDown(True)
                self.move_to_pointer(event)
                return
        if self.fine_drag and event.button() == Qt.MouseButton.LeftButton:
            self.drag_origin = event.position().x()
            self.setSliderDown(True)
            event.accept()
        else:
            super().mousePressEvent(event)

    def move_to_pointer(self, event) -> None:
        option = QStyleOptionSlider()
        self.initStyleOption(option)
        cc = QStyle.ComplexControl.CC_Slider
        groove = self.style().subControlRect(cc, option, QStyle.SubControl.SC_SliderGroove, self)
        handle = self.style().subControlRect(cc, option, QStyle.SubControl.SC_SliderHandle, self)
        span = max(1, groove.width() - handle.width())
        pos = round(event.position().x() - groove.left() - handle.width() / 2)
        self.setValue(
            QStyle.sliderValueFromPosition(self.minimum(), self.maximum(), pos, span, option.upsideDown)
        )
        event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self.groove_drag:
            self.move_to_pointer(event)
        elif self.drag_origin is not None:
            self.fine_moved.emit(event.position().x() - self.drag_origin)
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self.groove_drag and event.button() == Qt.MouseButton.LeftButton:
            self.move_to_pointer(event)
            self.groove_drag = False
            self.setSliderDown(False)
        elif self.drag_origin is not None and event.button() == Qt.MouseButton.LeftButton:
            self.drag_origin = None
            self.setSliderDown(False)
            event.accept()
        else:
            super().mouseReleaseEvent(event)
        if event.button() == Qt.MouseButton.LeftButton:
            self.mouse_active = False
            self.mouse_finished.emit()


class ScopeSlider(QWidget):
    committed = Signal(float)
    preview_changed = Signal(float)
    preview_reset = Signal()

    def __init__(self, title: str, unit: str, *, coarse=False, parent=None) -> None:
        super().__init__(parent)
        self.unit, self.coarse = unit, coarse
        self.steps: tuple[float, ...] = ()
        self.low, self.high, self.step = 0.0, 1.0, 0.01
        self.actual: float | None = None
        self.signature: tuple | None = None
        self.dirty = False
        self.tag_active = False
        self.fine = False
        self.division = 1.0
        self.drag_value = 0.0
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 2)
        outer.setSpacing(3)
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(8)
        self.setMinimumHeight(40)
        self.title = QLabel(title)
        self.title.setObjectName("scopeControlLabel")
        self.value_label = ReadingLabel("—")
        self.value_label.setObjectName("scopeControlValue")
        self.value_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        top.addWidget(self.title)
        top.addStretch(1)
        top.addWidget(self.value_label)
        outer.addLayout(top)
        self.slider = GestureSlider(Qt.Orientation.Horizontal)
        self.slider.linear = not coarse
        self.target: float | None = None
        self.slider.setAccessibleName(title)
        self.slider.setFixedHeight(18)
        outer.addWidget(self.slider)
        endpoints = QHBoxLayout()
        self.minimum_label, self.maximum_label = QLabel(), QLabel()
        for label in (self.minimum_label, self.maximum_label):
            label.setObjectName("rangeHint")
        endpoints.addWidget(self.minimum_label)
        endpoints.addStretch()
        endpoints.addWidget(self.maximum_label)
        # Range endpoints stay available as text and in the tooltip; the panel shows only the value.
        self.minimum_label.hide()
        self.maximum_label.hide()
        outer.addLayout(endpoints)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(180)
        self.timer.timeout.connect(self.commit)
        self.slider.valueChanged.connect(self.preview)
        self.slider.sliderPressed.connect(self.begin_gesture)
        self.slider.sliderReleased.connect(self.commit)
        self.slider.key_finished.connect(self.timer.start)
        self.slider.key_started.connect(self.timer.stop)
        self.slider.mouse_started.connect(self.begin_gesture)
        self.slider.mouse_finished.connect(self.commit)
        self.slider.gesture_cancelled.connect(self.restore_actual)
        self.slider.fine_step.connect(self.keyboard_step)
        self.slider.fine_moved.connect(self.fine_move)

    def begin_gesture(self) -> None:
        self.timer.stop()
        self.drag_value = self.actual if self.actual is not None else self.value()
        # The existing instrument value may now lie beyond a changed endpoint.
        # An explicit press/release can select that endpoint even without travel;
        # simply refreshing the range must never write it to the instrument.
        if not self.fine and self.actual is not None and not self.low <= self.actual <= self.high:
            self.dirty = True
            self.preview()

    def set_fine(self, enabled: bool) -> None:
        if self.coarse:
            return
        self.cancel()
        self.fine = self.slider.fine_drag = enabled

    def fine_move(self, pixels: float) -> None:
        if self.actual is None:
            return
        gain = min((self.high - self.low) / max(1, self.slider.width() - 20) / 10, self.division / 100)
        delta = math.trunc(pixels * gain / self.step) * self.step
        self.set_target(self.drag_value + delta)

    def set_target(self, value: float) -> None:
        self.target = min(self.high, max(self.low, float(f"{value:.12g}")))
        if self.actual is not None and math.isclose(
            self.target, self.actual, rel_tol=1e-12, abs_tol=self.step * 1e-12
        ):
            self.target = self.actual
        self.slider.blockSignals(True)
        self.slider.setValue(round((self.target - self.low) / (self.high - self.low) * self.slider.maximum()))
        self.slider.blockSignals(False)
        self.dirty = self.target != self.actual
        self.value_label.setText(("预计 " if self.dirty else "") + self.text(self.target))
        if self.dirty:
            self.preview_changed.emit(self.target)
        else:
            self.preview_reset.emit()

    def value(self) -> float:
        if self.coarse:
            return self.steps[self.slider.value()]
        if self.target is not None:
            return self.target
        if self.slider.value() == self.slider.minimum():
            return self.low
        if self.slider.value() == self.slider.maximum():
            return self.high
        raw = self.low + self.slider.value() / max(1, self.slider.maximum()) * (self.high - self.low)
        raw = round(raw / self.step) * self.step
        return min(self.high, max(self.low, float(f"{raw:.12g}")))

    def keyboard_step(self, direction: int) -> None:
        if self.actual is None:
            return
        initial = self.value() if self.dirty else self.actual
        self.set_target(initial + direction * self.step)

    def preview(self) -> None:
        self.target = None
        value = self.value()
        self.dirty = value != self.actual
        self.value_label.setText(("预计 " if self.dirty else "") + self.text(value))
        if self.dirty:
            self.preview_changed.emit(value)
        else:
            self.preview_reset.emit()
        if not self.slider.isSliderDown() and not self.slider.key_active and not self.slider.mouse_active:
            self.timer.start()

    def text(self, value: float) -> str:
        text = engineering(value, self.unit) + ("/格" if self.coarse else "")
        if self.coarse and value not in self.steps:
            return text + "（自定义）"
        return text

    def commit(self) -> None:
        self.timer.stop()
        if self.tag_active or not self.dirty:
            return
        self.dirty = False
        if self.isEnabled() and self.actual is not None:
            self.committed.emit(self.value())
        elif self.actual is not None:
            self.signature = None
            self.value_label.setText(self.text(self.actual))

    def cancel(self) -> None:
        self.timer.stop()
        self.tag_active = False
        self.dirty = False
        self.slider.key_active = False
        self.slider.mouse_active = False
        self.slider.drag_origin = None
        self.slider.groove_drag = False
        self.slider.blockSignals(True)
        self.slider.setSliderDown(False)
        self.slider.blockSignals(False)
        self.signature = None
        self.target = None

    def restore_actual(self) -> None:
        signature = self.signature
        self.cancel()
        if signature is not None:
            actual, spec, division, _fine = signature
            self.configure(actual, spec, division)
        self.preview_reset.emit()

    def configure(self, actual: float, spec: Parameter, division: float) -> bool:
        if self.dirty or self.slider.isSliderDown():
            return True
        if spec.minimum is None or spec.maximum is None or spec.maximum <= spec.minimum:
            return False
        signature = (actual, spec, division, self.fine)
        if self.signature == signature:
            return True
        self.actual, self.signature = actual, signature
        self.target = None
        self.low, self.high = spec.minimum, spec.maximum
        self.division = abs(division)
        self.slider.blockSignals(True)
        if self.coarse:
            self.steps = (
                tuple(sorted({float(v) for v in spec.choices if self.low <= float(v) <= self.high}))
                if spec.choices
                else coarse_steps(self.low, self.high)
            )
            if not self.steps:
                self.slider.blockSignals(False)
                return False
            self.slider.setRange(0, len(self.steps) - 1)
            self.slider.setValue(min(range(len(self.steps)), key=lambda i: abs(self.steps[i] - actual)))
            self.slider.setPageStep(10)
        else:
            wanted = max(abs(division) * (0.01 if self.fine else 0.1), 1e-15)
            self.step = (
                max(1, math.ceil(wanted / spec.resolution - 1e-10)) * spec.resolution
                if spec.resolution and spec.resolution > 0
                else wanted
            )
            ticks = min(2_000_000_000, max(1, math.ceil((self.high - self.low) / self.step)))
            self.slider.setRange(0, ticks)
            fraction = min(1.0, max(0.0, (actual - self.low) / (self.high - self.low)))
            self.slider.setValue(round(fraction * ticks))
            self.slider.setPageStep(10)
        self.slider.blockSignals(False)
        self.value_label.setText(self.text(actual))
        self.value_label.setToolTip(f"实际读回：{actual} {self.unit}")
        self.minimum_label.setText(engineering(self.steps[0] if self.coarse else self.low, self.unit))
        self.maximum_label.setText(engineering(self.steps[-1] if self.coarse else self.high, self.unit))
        self.slider.setToolTip(
            f"合法范围：{self.low:.12g} ～ {self.high:.12g} {self.unit} · 松手应用"
            + (f" · 键盘步进 {self.step:.12g} {self.unit}" if not self.coarse else "")
        )
        return True


class OscilloscopeControls(QFrame):
    busy_changed = Signal(bool)
    channels_changed = Signal()
    preview_requested = Signal(int, str, float)
    preview_cancelled = Signal()
    control_finished = Signal(bool)

    def __init__(self, controller, device_id: str, resume, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("scopeControls")
        self.controller, self.device_id, self.resume = controller, device_id, resume
        self.instrument_channels = tuple(controller.capabilities(device_id).channels)
        self.model_writes = {
            (ch, op.name)
            for ch in self.instrument_channels
            for op in controller.declared_capabilities(device_id, ch).operations
            if op.writable
        }
        self.running = False
        self.session_id = ""
        self.values: dict[tuple[int, str], Any] = {}
        self.sliders: dict[tuple[int, str], ScopeSlider] = {}
        self.checks: dict[int, QCheckBox] = {}
        self.pending: list[DeviceCommand] = []
        self.read_pending: list[tuple[int, tuple[str, ...]]] = []
        self.restart = False
        self.channel_change = False
        self.last_context: tuple | None = None
        self.point_limit_before: float | None = None
        self.point_clamp_attempted = False
        self.control_complete = False
        self.auto_notice = ""
        self.stop_revision = 0
        # Card: 参数 + sync state + refresh / 水平 · 垂直 · 触发 · 采集 tabs / fine + Auto footer.
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        head = QFrame(self)
        head.setObjectName("cardHead")
        head.setFixedHeight(52)
        heading = QHBoxLayout(head)
        heading.setContentsMargins(16, 0, 10, 0)
        heading.setSpacing(8)
        title = QLabel("参数", head)
        title.setObjectName("cardTitle")
        heading.addWidget(title)
        self.health = Pill("同步中", "warn", dot=True)
        heading.addWidget(self.health)
        heading.addStretch(1)
        self.refresh_button = QPushButton("刷新参数", head)
        style_button(self.refresh_button, "ghost", "refresh", "sm")
        self.refresh_button.setToolTip("读取仪器当前参数；前面板修改后请点击刷新。")
        self.refresh_button.clicked.connect(self.refresh_parameters)
        heading.addWidget(self.refresh_button)
        layout.addWidget(head)
        tab_row = QHBoxLayout()
        tab_row.setContentsMargins(14, 12, 14, 4)
        self.tabs = Segmented(())
        self.tabs.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        tab_row.addWidget(self.tabs)
        layout.addLayout(tab_row)
        self.scroller = QScrollArea()
        self.scroller.setWidgetResizable(True)
        self.scroller.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        body.setObjectName("cardBody")
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(0)
        self.pages = QStackedWidget(body)
        body_layout.addWidget(self.pages)
        body_layout.addStretch()
        self.tab_pages: dict[str, QVBoxLayout] = {}

        def tab(title: str) -> QVBoxLayout:
            if title not in self.tab_pages:
                page = QWidget()
                column = QVBoxLayout(page)
                column.setContentsMargins(0, 0, 0, 0)
                column.setSpacing(0)
                self.pages.addWidget(page)
                self.tabs.add_item(title, self.pages.count() - 1)
                self.tab_pages[title] = column
            return self.tab_pages[title]

        def section(title, page="水平"):
            frame = QFrame()
            frame.setObjectName("scopeSection")
            box = QVBoxLayout(frame)
            box.setContentsMargins(16, 10, 16, 12)
            box.setSpacing(6)
            if title:
                label = QLabel(title)
                label.setObjectName("sectionTitle")
                box.addWidget(label)
            tab(page).addWidget(frame)
            return box

        self.capture_settings = None
        tab("水平")
        tab("垂直")
        tab("触发")
        if {(1, "record_length"), (1, "record_length_auto")} <= self.model_writes:
            self.capture_settings = ScopeDepth()
            self.capture_settings.requested.connect(lambda cmd: self.submit([cmd], channels=True))
            section("采集设置", "采集").addWidget(self.capture_settings)
        self.tabs.changed.connect(self.pages.setCurrentIndex)
        self.tabs.set_current(0)

        horizontal = section("")
        self.add_slider(horizontal, 1, "timebase", "时基", "s", coarse=True)
        self.add_slider(horizontal, 1, "time_position", "偏移", "s")
        self.points: ScopePoints | None = None
        if self.capture_settings is None and (1, "record_length") in self.model_writes:
            point_row = QHBoxLayout()
            point_row.addWidget(QLabel("采集点数"))
            self.points = ScopePoints()
            self.points.setAccessibleName("采集点数")
            self.points.setMinimumWidth(115)
            self.points.setMaximumWidth(160)
            self.points.committed.connect(self.set_points)
            point_row.addWidget(self.points)
            self.points_range = QLabel("等待读回")
            self.points_range.setObjectName("rangeHint")
            self.points_range.setWordWrap(True)
            point_row.addWidget(self.points_range, 1)
            horizontal.addLayout(point_row)
        self.sample_rate = QLabel("采样率：—")
        self.sample_rate.setObjectName("scopeSampleRate")
        self.sample_rate.setAccessibleName("仪器采样率（只读）")
        self.sample_rate.setToolTip("仪器最近确认的采样率（缓存读回值）；随关联调参或主动刷新参数更新。")
        self._sample_rate_session = None
        self._sample_rate_value: float | None = None
        horizontal.addWidget(self.sample_rate)
        # 垂直: all channels stacked vertically on one scrollable page (no sub-tabs).
        vertical = tab("垂直")
        vertical.setContentsMargins(0, 4, 0, 4)
        vertical.setSpacing(0)
        vertical_page = self.pages.widget(list(self.tab_pages).index("垂直"))
        self.channel_sections: dict[int, QFrame] = {}
        self.channel_dots: dict[int, Dot] = {}
        self.focused_channel: int | None = None
        for idx, ch in enumerate(self.instrument_channels):
            frame = QFrame(vertical_page)
            frame.setObjectName("scopeChannelSection")
            frame.setProperty("last", idx == len(self.instrument_channels) - 1)
            box = QVBoxLayout(frame)
            box.setContentsMargins(14, 8, 16, 8)
            box.setSpacing(3)
            ch_head = QHBoxLayout()
            ch_head.setContentsMargins(0, 0, 0, 2)
            ch_head.setSpacing(7)
            dot = Dot(COLORS[(ch - 1) % len(COLORS)], size=8, box=12)
            self.channel_dots[ch] = dot
            ch_head.addWidget(dot)
            ch_title = QLabel(f"CH{ch}", frame)
            ch_title.setObjectName("scopeChannelTitle")
            ch_head.addWidget(ch_title)
            ch_head.addStretch(1)
            if (ch, "channel_enabled") in self.model_writes:
                check = Switch("", frame)
                check.setAccessibleName(f"CH{ch} 通道开启")
                check.setToolTip(f"开启或关闭 CH{ch}")
                check.clicked.connect(lambda enabled, c=ch: self.set_channel(c, enabled))
                ch_head.addWidget(check)
                self.checks[ch] = check
            box.addLayout(ch_head)
            self.add_slider(box, ch, "scale", "档位", "V", coarse=True)
            self.add_slider(box, ch, "offset", "偏移", "V")
            vertical.addWidget(frame)
            self.channel_sections[ch] = frame
        self.pages.currentChanged.connect(lambda _index: self._sync_page_heights())
        trigger = section("", "触发")
        row = QHBoxLayout()
        self.trigger_label = QLabel("触发 · 等待读回")
        self.trigger_label.setObjectName("spectrumChartTitle")
        row.addWidget(self.trigger_label, 1)
        self.gear: QToolButton | None = None
        if any(name in {"trigger_source", "trigger_slope", "trigger_mode"} for _, name in self.model_writes):
            self.gear = QToolButton()
            self.gear.setIcon(gear_icon())
            self.gear.setIconSize(QSize(20, 20))
            self.gear.setFixedSize(26, 24)
            self.gear.setAccessibleName("触发设置")
            self.gear.setToolTip("触发源、边沿和模式")
            self.gear.clicked.connect(self.trigger_dialog)
            row.addWidget(self.gear)
        trigger.addLayout(row)
        self.trigger_slider: ScopeSlider | None = None
        if any(name == "trigger_level" for _, name in self.model_writes):
            self.trigger_slider = ScopeSlider("电平", "V")
            self.trigger_slider.committed.connect(self.set_trigger_level)
            self.trigger_slider.preview_changed.connect(self.preview_trigger_level)
            self.trigger_slider.preview_reset.connect(self.preview_cancelled.emit)
            trigger.addWidget(self.trigger_slider)
        for column in self.tab_pages.values():
            column.addStretch(1)
        self.scroller.setWidget(body)
        layout.addWidget(self.scroller, 1)
        foot = QFrame(self)
        foot.setObjectName("cardFoot")
        foot_layout = QVBoxLayout(foot)
        foot_layout.setContentsMargins(14, 10, 14, 10)
        foot_layout.setSpacing(6)
        actions = QHBoxLayout()
        self.fine_button: QToolButton | None = None
        if any(not field.coarse for field in self.slider_fields()):
            self.fine_button = QToolButton()
            self.fine_button.setObjectName("scopeFineButton")
            self.fine_button.setProperty("buttonSize", "panel")
            self.fine_button.setText("微调")
            self.fine_button.setCheckable(True)
            self.fine_button.setToolTip("偏移和触发电平：普通 0.1 格，微调 0.01 格；微调拖动减速")
            self.fine_key = f"scope-fine/{controller.mode}/{device_id}"
            self.fine_button.setChecked(
                bool(controller.runtime.repository.load_workspace(self.fine_key, False))
            )
            for field in self.slider_fields():
                field.set_fine(self.fine_button.isChecked())
            self.fine_button.toggled.connect(self.change_fine)
            actions.addWidget(self.fine_button)
        self.auto_button: QPushButton | None = None
        if (1, "auto_setup") in self.model_writes:
            self.auto_button = QPushButton("Auto 自动设置")
            style_button(self.auto_button, "", "wand")
            self.auto_button.setProperty("buttonSize", "panel")
            self.auto_button.clicked.connect(self.auto_setup)
            actions.addWidget(self.auto_button, 1)
        foot_layout.addLayout(actions)
        self.status = QLabel(
            "拖动预览 · 松手写入仪器 · 也可直接拖动图上的 0 V 与 T 标签" if self.slider_fields() else ""
        )
        self.status.setObjectName("panelHint")
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status.setWordWrap(True)
        foot_layout.addWidget(self.status)
        layout.addWidget(foot)
        controller.refreshed.connect(self.refresh)
        self._sync_page_heights()
        self.refresh()

    def _sync_page_heights(self) -> None:
        current = self.pages.currentWidget()
        for index in range(self.pages.count()):
            page = self.pages.widget(index)
            if page is None:
                continue
            policy = QSizePolicy.Policy.Preferred if page is current else QSizePolicy.Policy.Ignored
            page.setSizePolicy(QSizePolicy.Policy.Preferred, policy)
        self.pages.adjustSize()

    def _scroll_to_focused(self) -> None:
        if self.focused_channel is None:
            return
        target = self.channel_sections.get(self.focused_channel)
        if target is not None and target.isVisible():
            self.scroller.ensureWidgetVisible(target, 0, 12)

    def show_channel(self, channel) -> None:
        """Focus and scroll to a channel block in the 垂直 tab."""
        if channel in self.channel_sections:
            self.focused_channel = channel
            for ch, frame in self.channel_sections.items():
                set_property(frame, "focused", ch == channel)
            QTimer.singleShot(0, self, self._scroll_to_focused)

    def show_tab(self, title: str) -> None:
        if title in self.tab_pages:
            index = list(self.tab_pages).index(title)
            self.pages.setCurrentIndex(index)
            self.tabs.set_current(index)

    def slider_fields(self) -> tuple[ScopeSlider, ...]:
        return (
            (*self.sliders.values(), self.trigger_slider)
            if self.trigger_slider
            else tuple(self.sliders.values())
        )

    def change_fine(self, enabled: bool) -> None:
        self.preview_cancelled.emit()
        self.controller.runtime.repository.save_workspace(self.fine_key, enabled)
        for field in self.slider_fields():
            field.set_fine(enabled)
        self.refresh()

    def auto_setup(self) -> None:
        self.preview_cancelled.emit()
        self.submit([DeviceCommand("auto_setup", confirmed=True)], channels=True)

    def add_slider(self, layout, channel, name, title, unit, *, coarse=False) -> None:
        if (channel, name) not in self.model_writes:
            return
        field = ScopeSlider(title, unit, coarse=coarse)
        self.sliders[channel, name] = field
        field.committed.connect(lambda value: self.submit([DeviceCommand(name, {"value": value}, channel)]))
        field.preview_changed.connect(lambda value: self.preview_requested.emit(channel, name, value))
        field.preview_reset.connect(self.preview_cancelled.emit)
        layout.addWidget(field)

    def capabilities(self, channel=1):
        return {c.name: c for c in self.controller.capabilities(self.device_id, channel).operations}

    def keys(self) -> tuple[tuple[int, str], ...]:
        return tuple(
            dict.fromkeys(
                (ch if cap.channel_scoped else 1, cap.name)
                for ch in self.instrument_channels
                for cap in self.capabilities(ch).values()
                if cap.readable
                and cap.available
                and (
                    cap.name in (*CONTROL_NAMES, "record_length", "coupling", "probe_attenuation")
                    or self.capture_settings is not None
                    and cap.name == "record_length_auto"
                )
            )
        )

    def channels(self) -> tuple[int, ...]:
        return tuple(
            ch for ch in self.instrument_channels if boolean_value(self.values.get((ch, "channel_enabled")))
        )

    def trigger_channel(self) -> int | None:
        source = str(self.values.get((1, "trigger_source"), ""))
        match = re.fullmatch(r"(?:C|CH|CHAN)([1-9])", source.upper())
        return int(match[1]) if match and int(match[1]) in self.instrument_channels else None

    def refresh(self) -> None:
        state = self.controller.states.get(self.device_id, {})
        self.refresh_sample_rate(state)
        context = (state.get("sessionId"), state.get("configurationVersion"))
        if context != self.last_context:
            if any(field.dirty or field.slider.isSliderDown() for field in self.slider_fields()):
                self.preview_cancelled.emit()
            self.values.clear()
            for field in self.slider_fields():
                field.cancel()
            self.last_context = context
        if not self.running:
            for key, frame in self.controller.runtime.sessions.readback_frames(self.device_id).items():
                cap = self.capabilities(key[0]).get(key[1])
                if (
                    (frame.session_id, frame.configuration_version) == context
                    and cap
                    and usable_reading(frame.reading, {} if key[1] == "trigger_source" else cap.parameters)
                ):
                    value = frame.reading.value
                    self.values[key] = value.get("value") if isinstance(value, dict) else value
        available = (
            state.get("connectionState") in {"connected", "busy"}
            and not state.get("controlsLocked")
            and not state.get("controlGroup")
            and not state.get("capabilityStale", True)
            and not self.running
            and self.device_id not in self.controller.group_devices
        )
        point_cap = self.capabilities().get("record_length")
        if self.capture_settings is not None:
            self.capture_settings.sync(context, self.values, self.capabilities(), available)
        point_value = self.values.get((1, "record_length"))
        if self.points is not None:
            if point_value is not None and point_cap and "value" in point_cap.parameters:
                self.points.configure(point_value, point_cap.parameters["value"])
            self.points.setEnabled(
                bool(
                    available
                    and point_cap
                    and point_cap.available
                    and point_cap.writable
                    and point_value is not None
                )
            )
            if point_cap and "value" in point_cap.parameters:
                point_spec = point_cap.parameters["value"]
                if point_spec.kind == "enum":
                    self.points_range.setText("")
                elif point_spec.minimum is not None and point_spec.maximum is not None:
                    self.points_range.setText(
                        f"{point_spec.minimum:,.0f}～{point_spec.maximum:,.0f}\n含软件限制"
                    )
                    if point_spec.choices:
                        self.points_range.setText(
                            self.points_range.text()
                            + "\n档位："
                            + " / ".join(
                                engineering(float(v), "").replace(" ", "") for v in point_spec.choices
                            )
                        )
                    self.points.setToolTip(
                        point_cap.reason
                        or "仪器最大采集长度；实际有效点数以收到的波形为准。超范围原值不自动修改。"
                    )
        for (ch, name), field in self.sliders.items():
            cap = self.capabilities(ch).get(name)
            actual = self.values.get((ch, name))
            division = (
                self.values.get((1, "timebase"), 1)
                if name.startswith("time")
                else self.values.get((ch, "scale"), 1)
            )
            valid = bool(cap and cap.available and actual is not None and "value" in cap.parameters)
            if valid and actual is not None:
                valid = field.configure(float(actual), cap.parameters["value"], float(division))
            enabled = name.startswith("time") or ch in self.channels()
            field.setEnabled(bool(available and valid and cap and cap.writable and enabled))
        for ch, check in self.checks.items():
            cap = self.capabilities(ch).get("channel_enabled")
            value = boolean_value(self.values.get((ch, "channel_enabled")))
            if value is not None:
                check.setChecked(value)
                if ch in self.channel_sections:
                    set_property(self.channel_sections[ch], "channelOff", not value)
                if ch in self.channel_dots:
                    self.channel_dots[ch].set_status(COLORS[(ch - 1) % len(COLORS)] if value else "idle")
            check.setEnabled(bool(available and value is not None and cap and cap.writable and cap.available))
        trigger_ch = self.trigger_channel()
        cap = self.capabilities(trigger_ch or 1).get("trigger_level")
        level_key = (trigger_ch or 1 if cap and cap.channel_scoped else 1, "trigger_level")
        level = self.values.get(level_key)
        valid = bool(trigger_ch and cap and cap.available and cap.writable and level is not None)
        if self.trigger_slider is not None:
            if valid and level is not None:
                valid = self.trigger_slider.configure(
                    float(level), cap.parameters["value"], self.values.get((trigger_ch or 1, "scale"), 1)
                )
            self.trigger_slider.setEnabled(bool(available and valid))
            self.trigger_slider.setToolTip(cap.reason if cap else "当前型号未提供触发电平")
        source = self.values.get((1, "trigger_source"), "—")
        mode = self.values.get((1, "trigger_mode"), "—")
        self.trigger_label.setText(f"触发 · {source} · {mode}")
        if self.gear is not None:
            self.gear.setEnabled(bool(available and self.values))
        auto = self.capabilities().get("auto_setup")
        if self.auto_button is not None:
            self.auto_button.setEnabled(bool(available and auto and auto.available and auto.writable))
            auto_running = self.running and any(c.operation == "auto_setup" for c in self.pending)
            self.auto_button.setText("自动设置中…" if auto_running else "Auto 自动设置")
            self.auto_button.setToolTip("执行仪器自动设置" if auto else "当前型号未提供自动设置")
        if self.fine_button is not None:
            self.fine_button.setEnabled(available)
        locked = bool(state.get("controlsLocked"))
        set_property(self, "locked", locked)
        reason = ""
        if self.running:
            self.health.set("设置中…", "warn", icon="", dot=True, live=True)
        elif locked:
            self.health.set("采集中 · 参数已锁定", "warn", icon="lock", dot=False)
        else:
            health, reason = self.controller.runtime.monitoring.settings_status(self.device_id, self.keys())
            show_sync_health(self.health, health)
        self.health.setToolTip(reason)
        self.refresh_button.setEnabled(available)

    def refresh_sample_rate(self, state: dict) -> None:
        """Render the published memory snapshot, never request a parameter read."""
        session = state.get("sessionId")
        if session != self._sample_rate_session:
            self._sample_rate_session = session
            self._sample_rate_value = None
        connected = bool(session) and state.get("connectionState") in {"connected", "busy", "degraded"}
        snapshot = self.controller.runtime.sessions.capability_snapshot(self.device_id)
        stale = connected and bool(
            not snapshot
            or snapshot.stale
            or state.get("capabilityStale")
            or state.get("connectionState") == "degraded"
        )
        if not connected:
            self._sample_rate_value = None
        elif snapshot and not stale and not self.running and not state.get("controlGroup"):
            memory = snapshot.details.get("memory", {})
            value = memory.get("sample_rate") if isinstance(memory, dict) else None
            try:
                rate = (
                    float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0
                )
            except OverflowError:
                rate = 0.0
            self._sample_rate_value = rate if math.isfinite(rate) and rate > 0 else None
        shown = engineering(self._sample_rate_value, "Sa/s") if self._sample_rate_value is not None else "—"
        self.sample_rate.setText(f"采样率：{shown}" + (" · 待更新" if stale else ""))
        if self.sample_rate.property("stale") != stale:
            self.sample_rate.setProperty("stale", stale)
            self.sample_rate.style().unpolish(self.sample_rate)
            self.sample_rate.style().polish(self.sample_rate)

    def set_channel(self, channel: int, enabled: bool) -> None:
        self.preview_cancelled.emit()
        cap = self.capabilities(channel)["channel_enabled"]
        value = enabled if cap.parameters["value"].kind == "boolean" else ("ON" if enabled else "OFF")
        self.submit([DeviceCommand("channel_enabled", {"value": value}, channel)], channels=True)

    def set_points(self, value: int | str) -> None:
        if self.running or self.points is None or not self.points.isEnabled():
            return
        try:
            if value != self.values.get((1, "record_length")):
                self.capabilities()["record_length"].validate({"value": value})
                self.submit([DeviceCommand("record_length", {"value": value})])
        except (ValueError, DeviceError) as exc:
            self.status.setText(f"采集点数未写入：{exc}")

    def trigger_state(self) -> tuple[int | None, float | None, bool]:
        """Trigger channel, its current level and whether the level can be dragged now."""
        channel = self.trigger_channel()
        if channel is None:
            return None, None, False
        cap = self.capabilities(channel).get("trigger_level")
        level = self.values.get((channel if cap and cap.channel_scoped else 1, "trigger_level"))
        if not isinstance(level, (int, float)) or isinstance(level, bool):
            return channel, None, False
        enabled = bool(self.trigger_slider is not None and self.trigger_slider.isEnabled())
        return channel, float(level), enabled

    def offset_channels(self) -> frozenset[int]:
        return frozenset(
            ch for (ch, name), field in self.sliders.items() if name == "offset" and field.isEnabled()
        )

    def preview_trigger_level(self, value: float) -> None:
        ch = self.trigger_channel()
        if ch is not None:
            self.preview_requested.emit(ch, "trigger_level", value)

    def set_trigger_level(self, value: float) -> None:
        ch = self.trigger_channel()
        if ch:
            cap = self.capabilities(ch)["trigger_level"]
            self.submit([DeviceCommand("trigger_level", {"value": value}, ch if cap.channel_scoped else 1)])

    def submit(self, commands: list[DeviceCommand], *, channels=False) -> None:
        if self.running:
            return
        try:
            self.controller.ensure_applicable(self.device_id)
            for cmd in commands:
                cap = self.controller.capabilities(self.device_id, cmd.channel).get(cmd.operation)
                if (
                    not cap.available
                    or not cap.writable
                    or (cmd.channel, cmd.operation) not in self.model_writes
                ):
                    raise DeviceError(cap.reason or "此设置暂不可写")
                cap.validate(cmd.parameters)
            self.controller.begin_group(self.device_id)
            point_cap = self.capabilities().get("record_length")
            self.point_limit_before = point_cap.parameters["value"].maximum if point_cap else None
            self.point_clamp_attempted = False
            self.control_complete = False
            self.auto_notice = ""
            self.stop_revision = self.controller.runtime.monitoring.stop_revision(self.device_id)
            self.session_id = self.controller.states[self.device_id]["sessionId"]
            self.running, self.channel_change = True, channels
            self.pending = list(commands)
            self.restart = channels and bool(
                self.controller.runtime.monitoring.snapshots().get(self.device_id, {}).get("running")
            )
            self.busy_changed.emit(True)
            self.refresh()
            if channels:
                self.controller.watch(
                    self.controller.runtime.monitoring.stop(self.device_id, preserve_intent=True),
                    lambda _: self.next_command(),
                    self.failed,
                )
            elif not commands:
                self.controller.watch(
                    self.controller.runtime.sessions.submit(self.device_id, "refresh_capabilities"),
                    lambda _: self.next_command(),
                    self.failed,
                )
            else:
                self.next_command()
        except Exception as exc:
            self.failed(exc)

    def valid_session(self) -> bool:
        return any(
            s["stableDeviceId"] == self.device_id and s["sessionId"] == self.session_id
            for s in self.controller.runtime.sessions.snapshots()
        )

    def next_command(self) -> None:
        if not self.valid_session():
            self.failed(DeviceError("会话已改变，停止后续操作"))
            return
        if self.pending:
            self.controller.apply(
                self.device_id, self.pending[0], self.applied, self.failed, group_member=True
            )
            return
        self.read_pending = []
        if not self.control_complete:
            for ch in self.instrument_channels:
                names = tuple(name for channel, name in self.keys() if channel == ch)
                self.read_pending.extend((ch, names[i : i + 4]) for i in range(0, len(names), 4))
        self.read_next()

    def applied(self, result) -> None:
        if result.outcome != "success":
            if result.outcome == "rejected" and result.operation == "auto_setup" and self.valid_session():
                state = next(
                    s
                    for s in self.controller.runtime.sessions.snapshots()
                    if s["stableDeviceId"] == self.device_id
                )
                if state["connectionState"] in {"connected", "busy"}:
                    self.auto_notice = f"Auto 未完成：{result.error or result.outcome}"
                    self.status.setText(self.auto_notice + " · 正在核对实际参数")
                    self.pending.clear()
                    self.control_complete = False
                    # Recovery from a rejection is strictly read-only. In particular,
                    # never clamp a memory length as a side effect of this check.
                    self.point_limit_before = None
                    self.controller.watch(
                        self.controller.runtime.sessions.submit(self.device_id, "refresh_capabilities"),
                        lambda _: self.next_command(),
                        self.failed,
                    )
                    return
            self.failed(DeviceError(result.error or result.outcome))
            return
        self.pending.pop(0)
        self.control_complete = bool(result.control_update and result.control_update.complete)
        self.next_command()

    def read_next(self) -> None:
        if not self.valid_session():
            self.failed(DeviceError("会话已改变，停止后续读取"))
        elif self.read_pending:
            ch, names = self.read_pending.pop(0)
            self.controller.watch(
                self.controller.runtime.sessions.submit(self.device_id, "read_settings", (names, ch)),
                lambda _: self.read_next(),
                self.failed,
            )
        else:
            try:
                if self.auto_notice:
                    frames = self.controller.runtime.sessions.readback_frames(self.device_id)
                    for ch, name in self.keys():
                        frame = frames.get((ch, name))
                        cap = self.capabilities(ch)[name]
                        if (
                            frame is None
                            or frame.session_id != self.session_id
                            or not usable_reading(
                                frame.reading, {} if name == "trigger_source" else cap.parameters
                            )
                        ):
                            raise DeviceError(f"Auto 后 {ch}.{name} 未可靠读回，监测未恢复")
                if not self.clamp_points_after_range_change():
                    self.finish()
            except Exception as exc:
                self.failed(exc)

    def clamp_points_after_range_change(self) -> bool:
        """Only explicit control groups may lower a value after its range shrinks."""
        cap = self.capabilities().get("record_length")
        if (
            self.points is None
            or not cap
            or not cap.available
            or not cap.writable
            or self.point_limit_before is None
        ):
            return False
        spec = cap.parameters["value"]
        maximum = spec.maximum
        if maximum is None or maximum >= self.point_limit_before:
            return False
        frame = self.controller.runtime.sessions.readback_frames(self.device_id).get((1, "record_length"))
        if (
            not frame
            or frame.session_id != self.session_id
            or not usable_reading(frame.reading, cap.parameters)
        ):
            raise DeviceError("点数范围已变化，但当前点数未可靠读回，请刷新核对")
        value = frame.reading.value
        current = normalized_value(value.get("value") if isinstance(value, dict) else value, spec)
        if current <= maximum:
            return False
        if self.point_clamp_attempted:
            raise DeviceError("自动降低点数后仍超出新范围，已停止后续设置")
        target = math.floor(maximum)
        if spec.choices:
            target = max(int(float(v)) for v in spec.choices if float(v) <= maximum)
        cap.validate({"value": target})
        self.point_clamp_attempted = True
        self.pending = [DeviceCommand("record_length", {"value": target})]
        self.next_command()
        return True

    def failed(self, exc) -> None:
        self.status.setText(f"未完成：{exc} · 未重发设置，请刷新核对")
        for field in self.slider_fields():
            field.cancel()
        if self.running:
            self.controller.watch(self.controller.runtime.monitoring.stop(self.device_id))
            self.finish(success=False)
        else:
            self.preview_cancelled.emit()
            self.refresh()

    def finish(self, *, success=True) -> None:
        restart = (
            self.restart
            and success
            and self.valid_session()
            and self.stop_revision == self.controller.runtime.monitoring.stop_revision(self.device_id)
        )
        self.running, self.restart = False, False
        self.pending.clear()
        self.controller.end_group(self.device_id, refresh=not (success and self.control_complete))
        self.busy_changed.emit(False)
        # Refresh reads are already cached by the worker; polling publishes the version.
        self.controller.states.update(
            {s["stableDeviceId"]: s for s in self.controller.runtime.sessions.snapshots()}
        )
        self.refresh()
        self.control_finished.emit(success)
        if success:
            self.status.setText(
                (self.auto_notice + " · 实际参数已核对" if self.auto_notice else "已核对实际参数")
                + (" · 单次模式：等待新波形" if self.values.get((1, "trigger_mode")) == "SINGLE" else "")
            )
            if self.channel_change:
                self.channels_changed.emit()
            if restart and self.channels():
                self.resume()

    def refresh_parameters(self) -> None:
        self.preview_cancelled.emit()
        self.submit([])

    def trigger_dialog(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("触发设置")
        dialog.setObjectName("sourceSettingsDialog")
        dialog.setMinimumWidth(320)
        form = QFormLayout(dialog)
        combos = {}
        caps = self.capabilities()
        source_cap = caps.get("trigger_source")
        current_source = self.values.get((1, "trigger_source"))
        source: WorkspaceComboBox | None = None
        if (1, "trigger_source") in self.model_writes:
            source = WorkspaceComboBox(dialog)
            for value in source_cap.parameters["value"].choices if source_cap else ():
                source.addItem(value, value)
            if current_source is not None and source.findData(current_source) < 0:
                source.addItem(str(current_source) + "（只读）", current_source)
            source.setCurrentIndex(source.findData(current_source))
            source.setEnabled(
                bool(
                    source_cap and source_cap.available and source_cap.writable and current_source is not None
                )
            )
            form.addRow("触发源", source)
            combos["trigger_source"] = source
        for name, title in (("trigger_slope", "边沿"), ("trigger_mode", "模式")):
            if not any(op == name for _, op in self.model_writes):
                continue
            combo = WorkspaceComboBox(dialog)
            form.addRow(title, combo)
            combos[name] = combo

        def update():
            match = re.fullmatch(
                r"(?:C|CH|CHAN)([1-9])", str(source.currentData() if source else current_source)
            )
            ch = int(match[1]) if match else None
            for name in ("trigger_slope", "trigger_mode"):
                if name not in combos:
                    continue
                combo = combos[name]
                cap = self.capabilities(ch or 1).get(name)
                combo.clear()
                for value in cap.parameters["value"].choices if cap else ():
                    combo.addItem(
                        {"POS": "上升沿", "NEG": "下降沿", "SINGLE": "单次等待", "STOP": "停止"}.get(
                            value, value
                        ),
                        value,
                    )
                target = (ch or 1) if cap and cap.channel_scoped else 1
                value = self.values.get((target, name))
                combo.setCurrentIndex(combo.findData(value))
                combo.setEnabled(bool(cap and cap.available and cap.writable and value is not None))

        if source is not None:
            source.currentIndexChanged.connect(update)
        update()
        notice = QLabel("仅修改本次选择的字段；不改变其他触发条件。")
        notice.setWordWrap(True)
        form.addRow(notice)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Apply | QDialogButtonBox.StandardButton.Cancel
        )
        form.addRow(buttons)
        buttons.button(QDialogButtonBox.StandardButton.Apply).clicked.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        context = self.last_context
        if dialog.exec() == QDialog.DialogCode.Accepted:
            try:
                if context != self.last_context:
                    raise DeviceError("弹窗打开期间参数已变化，请重新打开")
                selected = str(source.currentData() if source else current_source)
                match = re.fullmatch(r"(?:C|CH|CHAN)([1-9])", selected)
                ch = int(match[1]) if match else 1
                commands = []
                for name, combo in combos.items():
                    cap = self.capabilities(ch).get(name)
                    channel = ch if cap and cap.channel_scoped else 1
                    value = combo.currentData()
                    if combo.isEnabled() and value is not None and value != self.values.get((channel, name)):
                        commands.append(DeviceCommand(name, {"value": value}, channel, confirmed=True))
                if commands:
                    self.submit(commands)
            except DeviceError as exc:
                self.status.setText(str(exc))
        dialog.deleteLater()
