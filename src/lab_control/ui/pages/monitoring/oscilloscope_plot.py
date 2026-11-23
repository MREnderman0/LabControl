"""Fixed instrument grid; decimation affects the display, never the saved frame."""

from dataclasses import replace

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QFont, QTransform
from PySide6.QtWidgets import QApplication, QGraphicsItem, QLabel, QVBoxLayout

from lab_control.application.acquisition_pipeline import validate_displayed_batch
from lab_control.domain.instruments import DeviceError
from lab_control.domain.oscilloscope import COLORS, ScopeDisplay, engineering
from lab_control.ui.pages.monitoring.plot import LivePlot
from lab_control.ui.styles import tokens as tk


def waveform_measurements(values, axis) -> tuple[float, float, float | None]:
    """Vpp, RMS and a hysteresis-based frequency estimate of one displayed trace."""
    v = np.asarray(values, dtype=float)
    t = np.asarray(axis, dtype=float)
    finite = np.isfinite(v)
    if not finite.any():
        return float("nan"), float("nan"), None
    v_valid = v if finite.all() else v[finite]
    minimum = float(v_valid.min())
    vpp = float(v_valid.max() - minimum)
    rms = float(np.sqrt(np.mean(v_valid**2)))
    if vpp <= 0 or v.size < 8 or t.shape != v.shape or v_valid.size != v.size:
        return vpp, rms, None
    low = minimum + 0.3 * vpp
    return vpp, rms, crossing_frequency(v, t, low, low + 0.4 * vpp)


def crossing_frequency(v, t, low: float, high: float) -> float | None:
    """Time rising hysteresis crossings on native samples, carrying state across blocks."""
    was_high = True  # Arm only after observing the lower threshold.
    previous_crossing = None
    count = 0
    duration = squared_periods = 0.0
    block_size = 65_536
    for start in range(0, v.size, block_size):
        block = v[start : start + block_size]
        times = t[max(0, start - 1) : start + block.size]
        if not np.isfinite(times).all() or np.any(np.diff(times) <= 0):
            return None
        outside = np.flatnonzero((block <= low) | (block >= high))
        if not outside.size:
            continue
        states = block[outside] >= high
        rising = outside[states & ~np.r_[was_high, states[:-1]]] + start
        was_high = bool(states[-1])
        if not rising.size:
            continue
        before = rising - 1
        fraction = (high - v[before]) / (v[rising] - v[before])
        crossings = t[before] + fraction * (t[rising] - t[before])
        periods = np.diff(crossings if previous_crossing is None else np.r_[previous_crossing, crossings])
        previous_crossing = crossings[-1]
        count += periods.size
        duration += float(periods.sum())
        squared_periods += float(np.sum(periods**2))
    if count < 3 or duration <= 0:
        return None
    mean = duration / count
    # Suppress irregular crossings (noise); allow sampling jitter on short periods.
    if squared_periods / count > mean**2 * (1 + 0.15**2):
        return None
    return 1 / mean


class ScopeTag(pg.TextItem):
    """Screen tag in grid coordinates; vertical drags report the new level in divisions."""

    dragged = Signal(float, bool)
    pressed = Signal()
    cancelled = Signal()

    def __init__(self, text: str, color: str, anchor: tuple[float, float], font: QFont) -> None:
        super().__init__(text, anchor=anchor, color=tk.ACCENT_INK, fill=pg.mkBrush(color))
        self.setFont(font)
        self.setZValue(20)
        self.draggable = False
        self.dragging = False
        self.press_pos = None
        self.setAcceptedMouseButtons(Qt.MouseButton.LeftButton)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsFocusable)

    def set_draggable(self, draggable: bool) -> None:
        self.draggable = draggable
        if not draggable:
            self.cancel_drag()
        self.setCursor(Qt.CursorShape.SizeVerCursor if draggable else Qt.CursorShape.ArrowCursor)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if not self.draggable or event.button() != Qt.MouseButton.LeftButton:
            event.ignore()
            return
        event.accept()
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        self.press_pos = event.scenePos()
        self.press_level = self.pos().y()
        self.pressed.emit()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self.press_pos is None:
            return
        if (
            not self.dragging
            and (event.scenePos() - self.press_pos).manhattanLength() < QApplication.startDragDistance()
        ):
            return
        self.dragging = True
        self.move_to(event.scenePos(), False)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self.press_pos is None or event.button() != Qt.MouseButton.LeftButton:
            return
        if not self.dragging or event.scenePos().y() == self.press_pos.y():
            self.cancel_drag()
            return
        self.dragging = False
        self.move_to(event.scenePos(), True)
        self.press_pos = None

    def move_to(self, position, finished: bool) -> None:
        view = self.getViewBox()
        if view is not None and self.press_pos is not None:
            delta = view.mapSceneToView(position).y() - view.mapSceneToView(self.press_pos).y()
            y = max(-4.0, min(4.0, self.press_level + delta))
            self.setPos(self.pos().x(), y)
            self.dragged.emit(y, finished)

    def cancel_drag(self) -> None:
        if self.press_pos is not None:
            self.press_pos = None
            self.dragging = False
            self.cancelled.emit()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape and self.press_pos is not None:
            self.cancel_drag()
            event.accept()
            return
        super().keyPressEvent(event)


class OscilloscopePlot(LivePlot):
    tag_pressed = Signal(int, str)
    tag_cancelled = Signal()
    # Offset target in the instrument's convention (channel, value, released).
    ground_dragged = Signal(int, float, bool)
    # Trigger level target in volts (value, released).
    trigger_dragged = Signal(float, bool)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.preview_hint = QLabel()
        self.preview_hint.setObjectName("scopeSampleRate")
        self.preview_hint.hide()
        layout = self.layout()
        assert isinstance(layout, QVBoxLayout)
        layout.insertWidget(0, self.preview_hint)
        self.bottom_voltage = QLabel()
        self.bottom_voltage.setObjectName("scopeSampleRate")
        self.bottom_voltage.setWordWrap(True)
        layout.insertWidget(layout.indexOf(self.plot) + 1, self.bottom_voltage)
        self.bottom_voltage.hide()
        self.measurements = QLabel()
        self.measurements.setObjectName("scopeMeasurements")
        self.measurements.setToolTip("软件测量 · 基于当前显示帧的原始数据，不读取仪器测量项")
        self.measurements.setWordWrap(True)
        layout.insertWidget(layout.indexOf(self.bottom_voltage) + 1, self.measurements)
        self.measurements.hide()
        self.ground_tags: dict[int, ScopeTag] = {}
        self.trigger_tag: ScopeTag | None = None
        self.trigger_state: tuple[int | None, float | None, bool] = (None, None, False)
        self.offset_drag_channels: frozenset[int] = frozenset()
        self.display_specs: dict[int, ScopeDisplay] = {}
        self.plot.getViewBox().sigYRangeChanged.connect(self.update_shared_bottom)
        self.grid_lines: list = []
        self.grid_range: tuple | None = None
        self.instrument_view = False
        self.legend_signature: tuple | None = None
        self._restoring_grid = False
        self.preview_targets: dict[tuple[int, str], tuple[float, int | None]] = {}
        self.preview_session = ""
        self.preview_timer = QTimer(self)
        self.preview_timer.setSingleShot(True)
        self.preview_timer.setInterval(16)
        self.preview_timer.timeout.connect(self.render_preview)
        self.actual_meta = ""
        self.trigger_preview: pg.InfiniteLine | None = None
        self.plot.getViewBox().sigStateChanged.connect(self._keep_instrument_view)

    def reset(self) -> None:
        self.bottom_voltage.hide()
        self.bottom_voltage.clear()
        self.measurements.hide()
        self.measurements.clear()
        self.ground_tags.clear()
        self.trigger_tag = None
        self.display_specs.clear()
        self.preview_hint.hide()
        self.preview_timer.stop()
        self.preview_targets.clear()
        self.instrument_view = False
        self.grid_range = None
        super().reset()
        self.grid_lines.clear()
        self.trigger_preview = None
        self.grid_range = None
        self.instrument_view = False
        self.legend_signature = None

    def show_bottom(self, values, preview=False):
        labels = []
        for ch, value, unit in sorted(values):
            number, scaled_unit = engineering(value, unit).split(" ", 1)
            if "e" not in number and len(number.partition(".")[2]) < 2:
                number = f"{float(number):.2f}"
            number = number.replace("-", "−")
            color = COLORS[(ch - 1) % len(COLORS)]
            labels.append(f'<span style="color:{color}">CH{ch} {number} {scaled_unit}</span>')
        self.bottom_voltage.setText(("预览 · " if preview else "") + "底边电压　" + "　".join(labels))
        self.bottom_voltage.setVisible(bool(labels))

    def update_shared_bottom(self, *_):
        if self.instrument_view or self.displayed_frame is None:
            return
        bottom = self.plot.getViewBox().viewRange()[1][0]
        self.show_bottom([(m.channel, bottom, m.unit) for m in self.displayed_frame.batch.measurements])

    def display_spec(self, measurement, batch):
        if "scope_display" in measurement.metadata:
            return ScopeDisplay.parse(measurement.metadata["scope_display"])
        if batch.identity.model.upper() != "6404E":
            return None
        config = batch.configuration
        settings = config.get("channels", {})
        channel = settings.get(measurement.channel, settings.get(str(measurement.channel)))
        if channel is None or measurement.axis is None or not len(measurement.axis):
            return None
        start, stop = float(measurement.axis[0]), float(measurement.axis[-1])
        duration = stop - start if len(measurement.axis) > 1 else config["sample_interval_s"]
        return ScopeDisplay.parse(
            dict(
                units_per_div=float(channel["range"]) / 4,
                reference_value=-float(channel["offset"]),
                seconds_per_div=duration / 10,
                time_center=(start + stop) / 2,
            )
        )

    def show_frame(self, frame, source, channels) -> None:
        wanted = tuple(m for m in frame.batch.measurements if m.channel in channels)
        if not wanted:
            return
        candidate = replace(frame, batch=replace(frame.batch, measurements=wanted))
        try:
            validate_displayed_batch(candidate.batch)
        except DeviceError:
            return
        try:
            displays = [self.display_spec(m, frame.batch) for m in wanted]
        except (DeviceError, KeyError, TypeError, ValueError):
            return
        if all(spec is None for spec in displays):
            if self.instrument_view:
                self.reset()
            self.plot.showAxis("left")
            self.plot.showGrid(x=True, y=True, alpha=0.16)
            self.plot.setMouseEnabled(x=True, y=True)
            self.plot.setMenuEnabled(True)
            super().show_frame(frame, source, channels)
            self.update_shared_bottom()
            return
        try:
            if any(spec is None for spec in displays):
                return
            first = displays[0]
            for m, spec in zip(wanted, displays, strict=True):
                if m.axis is None or m.axis_unit != "s" or m.unit != spec.unit:
                    raise DeviceError("Waveform/display units differ", "parse_error")
                if (spec.seconds_per_div, spec.time_center, spec.horizontal_divisions) != (
                    first.seconds_per_div,
                    first.time_center,
                    first.horizontal_divisions,
                ):
                    raise DeviceError("Waveform horizontal displays differ", "parse_error")
            with np.errstate(over="ignore", invalid="ignore"):
                coordinates = [
                    (np.asarray(m.values) - spec.reference_value) / spec.units_per_div
                    for m, spec in zip(wanted, displays, strict=True)
                ]
            if any(not np.isfinite(y).all() for y in coordinates):
                raise DeviceError("Non-finite display coordinates", "parse_error")
        except DeviceError:
            return
        token = (frame.session_id, frame.sequence, frame.configuration_version, frame.received_at, channels)
        if token == self.last_token:
            return
        if self.preview_session != frame.session_id:
            self.preview_targets.clear()
        self.preview_targets = {
            key: target
            for key, target in self.preview_targets.items()
            if target[1] is None or target[1] > frame.configuration_version
        }
        if not self.instrument_view:
            self.reset()
            self.instrument_view = True
        self.plot_item.one_shot_fit = True
        self.plot_item.hideButtons()
        self.last_token = token
        bounds = (
            first.time_center - first.horizontal_divisions / 2 * first.seconds_per_div,
            first.time_center + first.horizontal_divisions / 2 * first.seconds_per_div,
        )
        # Establish the microsecond viewport before pyqtgraph computes downsampling.
        if bounds != self.grid_range:
            self.grid_range = bounds
            self.fit_current()
        self._keep_instrument_view()
        self.plot.hideAxis("left")
        self.plot.setLabel("bottom", "时间", units="s")
        self.plot.showGrid(x=False, y=False)
        self.plot.setMouseEnabled(x=False, y=False)
        self.plot.setMenuEnabled(True)
        self.value.hide()
        horizontal_lines = first.horizontal_divisions + 1
        if self.grid_lines and len(self.grid_lines) != horizontal_lines + 9:
            for line in self.grid_lines:
                self.plot.removeItem(line)
            self.grid_lines.clear()
        if not self.grid_lines:
            for angle, count in ((90, horizontal_lines), (0, 9)):
                for index in range(count):
                    alpha = 110 if index == count // 2 else 78
                    pen = pg.mkPen((110, 113, 121, alpha), width=1, style=Qt.PenStyle.DashLine)
                    line = pg.InfiniteLine(angle=angle, pen=pen, movable=False)
                    line.setZValue(-10)
                    self.plot.addItem(line, ignoreBounds=True)
                    self.grid_lines.append(line)
        for line, x in zip(
            self.grid_lines[:horizontal_lines], np.linspace(*self.grid_range, horizontal_lines), strict=True
        ):
            line.setPos(float(x))
        for line, grid_y in zip(self.grid_lines[horizontal_lines:], range(-4, 5), strict=True):
            line.setPos(grid_y)
        used = set()
        legend = self.plot.plotItem.legend
        signature = tuple(
            (m.channel, spec.units_per_div, m.unit) for m, spec in zip(wanted, displays, strict=True)
        )
        update_legend = signature != self.legend_signature
        if update_legend:
            legend.clear()
            self.legend_signature = signature
        for m, spec, y in zip(wanted, displays, coordinates, strict=True):
            key = (m.channel, m.quantity)
            color = COLORS[(m.channel - 1) % len(COLORS)]
            if key not in self.curves:
                curve = self.plot.plot(pen=pg.mkPen(color, width=1.2))
                curve.setDownsampling(auto=True, method="peak")
                curve.setClipToView(True)
                self.curves[key] = curve
            curve = self.curves[key]
            curve.setTransform(QTransform())
            curve.setData(
                m.axis,
                y,
                connect="finite",
                symbol="o" if len(m.values) == 1 else None,
                useCache=False,
                symbolSize=5,
            )
            curve.show()
            if update_legend:
                legend.addItem(
                    curve,
                    f'<span style="color:{color}">CH{m.channel}  {engineering(spec.units_per_div, m.unit)}/格</span>',
                )
            used.add(key)
        for key, curve in self.curves.items():
            if key not in used:
                curve.hide()
        self._place_tags({m.channel: spec for m, spec in zip(wanted, displays, strict=True)})
        self._show_measurements(wanted)
        self.meta.setText(
            f"{engineering(first.seconds_per_div, 's')}/格 · "
            + " / ".join(f"CH{m.channel} {len(m.values)} 点" for m in wanted)
            + (
                {"screen": " · 屏幕波形", "memory": " · 完整存储波形"}.get(
                    wanted[0].metadata.get("waveform_extent"), ""
                )
            )
        )
        self.meta.setToolTip(f"配置版本 {frame.configuration_version} · 校验 {frame.batch.validation}")
        self.displayed_frame = candidate
        self.display_specs = {m.channel: s for m, s in zip(wanted, displays, strict=True)}
        self.show_bottom(
            [(ch, s.reference_value - 4 * s.units_per_div, s.unit) for ch, s in self.display_specs.items()]
        )
        self.actual_meta = self.meta.text()
        if self.trigger_preview is not None:
            self.trigger_preview.hide()
        if self.preview_targets:
            self.render_preview()

    def preview_parameter(self, channel: int, operation: str, value: float) -> None:
        """Estimated viewport only: never replace the immutable exported frame."""
        labels = {
            "timebase": ("时基", "s", "/格"),
            "time_position": ("水平偏移", "s", ""),
            "scale": (f"CH{channel} 档位", "V", "/格"),
            "offset": (f"CH{channel} 偏移", "V", ""),
            "trigger_level": (f"CH{channel} 触发电平", "V", ""),
        }
        if operation not in labels or not np.isfinite(value):
            return
        if operation in {"scale", "timebase"} and value <= 0:
            return
        label, unit, suffix = labels[operation]
        target = engineering(value, unit) + suffix
        self.preview_hint.setText(
            f"预计{label}  {target}" + (" · 等待首帧" if self.displayed_frame is None else "")
        )
        self.preview_hint.show()
        if not self.instrument_view or self.displayed_frame is None:
            return
        self.preview_session = self.displayed_frame.session_id
        self.preview_targets[channel, operation] = (value, None)
        if not self.preview_timer.isActive():
            self.preview_timer.start()

    def commit_preview(self, version: int) -> None:
        self.preview_hint.hide()
        self.preview_targets = {
            key: (value, version if pending is None else pending)
            for key, (value, pending) in self.preview_targets.items()
        }
        self.render_preview()

    def clear_preview(self) -> None:
        self.preview_hint.hide()
        if not self.preview_targets:
            self.fit_current()
            return
        self.preview_timer.stop()
        self.preview_targets.clear()
        self.render_preview()

    def render_preview(self) -> None:
        if not self.instrument_view or self.displayed_frame is None:
            return
        specs = []
        used = False
        if self.trigger_preview is not None:
            self.trigger_preview.hide()
        for measurement in self.displayed_frame.batch.measurements:
            original = self.display_specs[measurement.channel]
            ch = measurement.channel
            targets = self.preview_targets
            scale = targets.get((ch, "scale"), (original.units_per_div, None))[0]
            timebase = targets.get((1, "timebase"), (original.seconds_per_div, None))[0]
            reference = original.reference_value + original.vertical_position_divisions * (
                original.units_per_div - scale
            )
            if (ch, "offset") in targets and original.vertical_offset_sign:
                reference = original.vertical_offset_sign * targets[ch, "offset"][0] - (
                    original.vertical_position_divisions * scale
                )
                used = True
            reference_divs = (
                (50 - original.horizontal_reference_percent) / 100 * original.horizontal_divisions
            )
            center = original.time_center + reference_divs * (timebase - original.seconds_per_div)
            if (1, "time_position") in targets and original.horizontal_position_sign:
                center = (
                    original.horizontal_position_sign * targets[1, "time_position"][0]
                    + reference_divs * timebase
                )
                used = True
            used |= (ch, "scale") in targets or (1, "timebase") in targets
            try:
                spec = replace(
                    original,
                    units_per_div=scale,
                    reference_value=reference,
                    seconds_per_div=timebase,
                    time_center=center,
                )
            except ValueError:
                return
            specs.append((measurement, spec))
            if (ch, "trigger_level") in targets:
                level = targets[ch, "trigger_level"][0]
                if self.trigger_preview is None:
                    self.trigger_preview = pg.InfiniteLine(
                        angle=0, movable=False, pen=pg.mkPen(tk.ACCENT, style=Qt.PenStyle.DashLine)
                    )
                    self.trigger_preview.setZValue(5)
                    self.plot.addItem(self.trigger_preview, ignoreBounds=True)
                self.trigger_preview.setPos((level - reference) / scale)
                self.trigger_preview.show()
                used = True
            curve = self.curves.get((measurement.channel, measurement.quantity))
            if curve is not None:
                # Affine scene transform reuses the existing curve/decimation;
                # mouse movement does not allocate or rewrite full sample arrays.
                transform = QTransform()
                transform.translate(0, (original.reference_value - spec.reference_value) / spec.units_per_div)
                transform.scale(1, original.units_per_div / spec.units_per_div)
                curve.setTransform(transform)
        if not specs:
            return
        self._place_tags({m.channel: s for m, s in specs})
        self.show_bottom(
            [(m.channel, s.reference_value - 4 * s.units_per_div, s.unit) for m, s in specs], used
        )
        first = specs[0][1]
        self.grid_range = (
            first.time_center - first.horizontal_divisions / 2 * first.seconds_per_div,
            first.time_center + first.horizontal_divisions / 2 * first.seconds_per_div,
        )
        self.fit_current()
        horizontal_lines = first.horizontal_divisions + 1
        for line, x in zip(
            self.grid_lines[:horizontal_lines], np.linspace(*self.grid_range, horizontal_lines), strict=True
        ):
            line.setPos(float(x))
        signature = tuple((m.channel, spec.units_per_div, m.unit) for m, spec in specs)
        if signature != self.legend_signature:
            legend = self.plot.plotItem.legend
            legend.clear()
            for m, spec in specs:
                color = COLORS[(m.channel - 1) % len(COLORS)]
                legend.addItem(
                    self.curves[m.channel, m.quantity],
                    f'<span style="color:{color}">CH{m.channel}  {engineering(spec.units_per_div, m.unit)}/格</span>',
                )
            self.legend_signature = signature
        self.meta.setText(
            (
                "拖动预览 · 估算，松手应用"
                if any(v is None for _, v in self.preview_targets.values())
                else "预览 · 估算，等待实测新帧"
            )
            + f" · {engineering(first.seconds_per_div, 's')}/格 · 基于上一帧，保存仍用原始数据"
            if used
            else self.actual_meta
        )

    def _tag_font(self) -> QFont:
        font = QFont(self.font())
        font.setPixelSize(11)
        font.setBold(True)
        return font

    def set_drag_state(
        self, offset_channels: frozenset[int], trigger: tuple[int | None, float | None, bool]
    ) -> None:
        """Which tags may be dragged now (from the control panel's enabled sliders)."""
        if (offset_channels, trigger) == (self.offset_drag_channels, self.trigger_state):
            return
        self.offset_drag_channels, self.trigger_state = offset_channels, trigger
        if self.display_specs:
            self._place_tags(self.display_specs)

    def _place_tags(self, specs: dict) -> None:
        """0 V tags on the left edge per channel; the trigger level on the right edge."""
        shown = set()
        left = self.grid_range[0] if self.grid_range else 0.0
        right = self.grid_range[1] if self.grid_range else 0.0
        step = (right - left) * 0.024
        placed: list[float] = []
        for channel, spec in sorted(specs.items()):
            level = (0 - spec.reference_value) / spec.units_per_div
            arrow = "▲" if level > 3.8 else "▼" if level < -3.8 else ""
            tag = self.ground_tags.get(channel)
            if tag is None:
                tag = ScopeTag("", COLORS[(channel - 1) % len(COLORS)], (0, 0.5), self._tag_font())
                tag.pressed.connect(lambda c=channel: self.tag_pressed.emit(c, "offset"))
                tag.cancelled.connect(self.tag_cancelled)
                tag.dragged.connect(lambda y, done, c=channel: self._ground_dragged(c, y, done))
                self.plot.addItem(tag, ignoreBounds=True)
                self.ground_tags[channel] = tag
            draggable = channel in self.offset_drag_channels and spec.vertical_offset_sign in {-1, 1}
            tag.set_draggable(draggable)
            tag.setText(f"{channel}{arrow}")
            tag.setToolTip(f"CH{channel} 0 V" + (" · 上下拖动调整偏移，松手写入仪器" if draggable else ""))
            y = max(-3.8, min(3.8, level))
            stacked = sum(abs(y - other) < 0.4 for other in placed)  # Same level: tags sit side by side.
            placed.append(y)
            if not tag.dragging:
                tag.setPos(left + stacked * step, y)
            tag.show()
            shown.add(channel)
        for channel, tag in self.ground_tags.items():
            if channel not in shown:
                tag.hide()
        trigger_channel, level, enabled = self.trigger_state
        spec = specs.get(trigger_channel) if trigger_channel is not None else None
        if spec is None or level is None or trigger_channel is None:
            if self.trigger_tag is not None:
                self.trigger_tag.hide()
            return
        if self.trigger_tag is None:
            self.trigger_tag = ScopeTag("T", tk.TEXT_2, (1, 0.5), self._tag_font())
            self.trigger_tag.pressed.connect(
                lambda: self.tag_pressed.emit(self.trigger_state[0], "trigger_level")
            )
            self.trigger_tag.cancelled.connect(self.tag_cancelled)
            self.trigger_tag.dragged.connect(self._trigger_dragged)
            self.plot.addItem(self.trigger_tag, ignoreBounds=True)
        tag = self.trigger_tag
        tag.fill = pg.mkBrush(COLORS[(trigger_channel - 1) % len(COLORS)])
        y = (level - spec.reference_value) / spec.units_per_div
        arrow = "▲" if y > 3.8 else "▼" if y < -3.8 else ""
        tag.setText(f"T{arrow}")
        tag.set_draggable(enabled)
        tag.setToolTip(
            f"触发电平 CH{trigger_channel} {engineering(level, spec.unit)}"
            + (" · 上下拖动调整，松手写入仪器" if enabled else "")
        )
        if not tag.dragging:
            tag.setPos(right, max(-3.8, min(3.8, y)))
        tag.show()

    def _ground_dragged(self, channel: int, y: float, finished: bool) -> None:
        spec = self.display_specs.get(channel)
        if spec is None or spec.vertical_offset_sign not in {-1, 1}:
            return
        # reference = sign * offset - position_divs * scale, and 0 V sits at -reference / scale.
        offset = spec.vertical_offset_sign * (spec.vertical_position_divisions - y) * spec.units_per_div
        self.ground_dragged.emit(channel, offset, finished)

    def _trigger_dragged(self, y: float, finished: bool) -> None:
        channel = self.trigger_state[0]
        spec = self.display_specs.get(channel) if channel is not None else None
        if spec is not None:
            self.trigger_dragged.emit(y * spec.units_per_div + spec.reference_value, finished)

    def _show_measurements(self, measurements) -> None:
        parts = []
        for m in measurements:
            if m.axis is None:
                continue
            vpp, rms, frequency = waveform_measurements(m.values, m.axis)
            if not np.isfinite(vpp):
                continue
            color = COLORS[(m.channel - 1) % len(COLORS)]
            values = [f"Vpp {engineering(vpp, m.unit)}", f"RMS {engineering(rms, m.unit)}"]
            values.append(f"{engineering(frequency, 'Hz')}" if frequency else "频率 —")
            text = "&nbsp;·&nbsp;".join(value.replace(" ", "&nbsp;") for value in values)
            parts.append(
                f'<span style="color:{color}; font-weight:600">CH{m.channel}</span>'
                f'&nbsp;&nbsp;<span style="color:{tk.TEXT_2}">{text}</span>'
            )
        # Wrap only between channels, never inside a value.
        self.measurements.setText("&nbsp;&nbsp;&nbsp; ".join(parts).replace("-", "−"))
        self.measurements.setVisible(bool(parts))

    def restrict_channels(self, channels: tuple[int, ...]) -> None:
        """Drop explicitly disabled traces without changing the old frame's scales."""
        if self.displayed_frame is None:
            return
        remaining = tuple(m.channel for m in self.displayed_frame.batch.measurements if m.channel in channels)
        if len(remaining) == len(self.displayed_frame.batch.measurements):
            return
        if remaining:
            from lab_control.application.controls import DataSource

            self.show_frame(self.displayed_frame, DataSource("waveform", "时域波形", "waveform"), remaining)
        else:
            for curve in self.curves.values():
                curve.hide()
            self.bottom_voltage.hide()
            self.measurements.hide()
            for tag in (*self.ground_tags.values(), self.trigger_tag):
                if tag is not None:
                    tag.hide()
            self.display_specs.clear()
            self.plot.plotItem.legend.clear()
            self.legend_signature = None
            self.displayed_frame = None
            self.last_token = None
            self.meta.setText("所选通道已关闭 · 无显示波形")

    def _keep_instrument_view(self, *_args) -> None:
        if not self.instrument_view or not self.grid_range or self._restoring_grid:
            return
        view = self.plot.getViewBox()
        x, y = view.viewRange()
        if any(view.autoRangeEnabled()) or tuple(x) != self.grid_range or tuple(y) != (-4, 4):
            self.fit_current()

    def fit_current(self) -> None:
        if self.grid_range:
            self._restoring_grid = True
            try:
                self.plot.setRange(xRange=self.grid_range, yRange=(-4, 4), padding=0, disableAutoRange=True)
            finally:
                self._restoring_grid = False
        else:
            super().fit_current()
