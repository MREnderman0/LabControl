"""Grouped parameter cards for the Moku measurement workspaces."""

from math import isfinite

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QTabBar,
    QVBoxLayout,
    QWidget,
)

from lab_control.domain.instruments import DeviceCommand, Parameter, ValidationError
from lab_control.domain.oscilloscope import engineering
from lab_control.ui.pages.device_control.editors import UNIT_CHOICES, ParameterEditor
from lab_control.ui.pages.monitoring.oscilloscope_controls import ScopeSlider
from lab_control.ui.widgets.workspace_combo import WorkspaceComboBox

from .adapter import FRONTEND
from .capture_settings import ACK_ONLY, CAPTURE, FREQUENCY, TOKENS, field, wire_parameters
from .display_controls import DisplayControls


def display_value(editor, value):
    scales = UNIT_CHOICES.get(editor.spec.unit)
    if scales and type(value) in (int, float) and not editor.spec.choices:
        factor = editor.unit.currentData() or 1
        if not editor.unit_selected and value:
            factor = max((scale for _, scale in scales if scale <= abs(value)), default=scales[0][1])
        editor.restore({"value": f"{value / factor:.12g}", "factor": factor}, readback=True)
    else:
        editor.set_value(value, readback=True)


class CaptureControls(QFrame):
    def __init__(self, page):
        super().__init__()
        self.setObjectName("scopeControls" if page.kind == "waveform" else "spectrumControls")
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(6, 0, 0, 0)
        self.body.setSpacing(6)
        header = QHBoxLayout()
        header.addWidget(QLabel("参数"))
        self.body.addLayout(header)
        self.page = page
        self.scope = page.kind == "waveform"
        self.editors = {}
        self.front_fields = {}
        self.defaults = {}
        self.dirty = {}
        self.context = None
        self.active = None
        self.reading = None
        self.writable = False
        self.frequency_pair = None
        self.display = DisplayControls(page)
        self.feedback = QLabel("等待读回")
        self.feedback.setObjectName("spectrumHealth")
        self.feedback.setFixedHeight(22)
        header.addWidget(self.feedback)
        header.addStretch()
        self.refresh_button = QPushButton("刷新参数")
        self.refresh_button.clicked.connect(self.refresh)
        header.addWidget(self.refresh_button)
        self.receipt = QLabel()
        self.receipt.setObjectName("inputFeedback")
        self.receipt.setWordWrap(True)
        self.scroller = QScrollArea()
        self.scroller.setWidgetResizable(True)
        self.scroller.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        self.column = QVBoxLayout(body)
        self.column.setContentsMargins(0, 0, 0, 0)
        self.column.setSpacing(5)
        self.scroller.setWidget(body)
        if self.scope:
            self.build_scope()
        else:
            self.build_spectrum()
        self.column.addStretch()
        self.body.addWidget(self.scroller, 1)
        foot = QHBoxLayout()
        self.body.addLayout(foot)
        foot.addWidget(self.receipt, 1)
        self.apply_button = QPushButton("应用修改")
        self.apply_button.setObjectName("primaryButton")
        self.apply_button.clicked.connect(self.apply)
        foot.addWidget(self.apply_button)
        self.setMinimumWidth(330)
        self.setMaximumWidth(430)

    def section(self, label, parent):
        section = QFrame()
        section.setObjectName("scopeSection")
        box = QVBoxLayout(section)
        if label:
            title = QLabel(label)
            title.setObjectName("spectrumChartTitle")
            box.addWidget(title)
        box.setContentsMargins(15, 8, 15, 9)
        box.setSpacing(4)
        parent.addWidget(section)
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(3)
        for col in (0, 1):
            grid.setColumnStretch(col, 1)
        box.addLayout(grid)
        return grid

    def cell(self, grid, index, title, widget, extra=None):
        row, col = divmod(index, 2)
        heading = QHBoxLayout()
        label = QLabel(title)
        label.setObjectName("controlTitle")
        heading.addWidget(label)
        heading.addStretch()
        if extra is not None:
            heading.addWidget(extra)
        widget.setAccessibleName(title)
        widget.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        widget.setMinimumWidth(0)
        grid.addLayout(heading, row * 2, col)
        grid.addWidget(widget, row * 2 + 1, col)

    def editor(self, grid, index, group, key, title, default, spec=None):
        editor = ParameterEditor(
            spec or CAPTURE[group][1][key], simplified=True, label=title, show_range=False
        )
        if editor.spec.kind == "enum" and isinstance(editor.input, QComboBox):
            for i, token in enumerate(editor.spec.choices):
                editor.input.setItemText(
                    i, {"CURRENT": "当前通道", "EXTERNAL": "外部"}.get(token, TOKENS[token])
                )
        if key == "max_length":
            editor.unit.hide()
        display_value(editor, default)
        self.cell(grid, index, title, editor)
        self.editors.setdefault(group, {})[key] = editor
        self.defaults[group, key] = default
        editor.changed.connect(lambda: self.edit(group, key))
        return editor

    def front(self, grid, index, key, label):
        box = WorkspaceComboBox()
        box.addItem("保持当前", None)
        for value in FRONTEND[key]:
            box.addItem(value, value)
        self.cell(grid, index, label, box)
        self.front_fields[key] = box
        box.currentIndexChanged.connect(self.buttons)

    def build_scope(self):
        names = ("水平", "垂直", "触发", "采集")
        self.tabs = QTabBar()
        self.tabs.setExpanding(True)
        for name in names:
            self.tabs.addTab(name)
        self.tabs.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        bar = QHBoxLayout()
        bar.setContentsMargins(14, 10, 14, 4)
        bar.addWidget(self.tabs)
        self.body.addLayout(bar)
        self.pages = QStackedWidget()
        self.column.addWidget(self.pages)
        self.tabs.currentChanged.connect(self.pages.setCurrentIndex)
        self.tabs.setCurrentIndex(0)
        grids = []
        for name in names:
            tab = QWidget()
            layout = QVBoxLayout(tab)
            layout.setContentsMargins(0, 0, 0, 0)
            grids.append(self.section("", layout))
            layout.addStretch()
            self.pages.addWidget(tab)
        self.range_group = "scope_timebase"
        self.endpoints = ("t1", "t2")
        self.alternate = ("seconds_per_div", "center")
        h, v, t, a = grids
        self.editor(
            h, 0, self.range_group, "seconds_per_div", "时基（每格）", 0.0002, Parameter(unit="s", minimum=0)
        )
        self.editor(h, 1, self.range_group, "center", "水平偏移", 0, Parameter(unit="s"))
        self.editor(h, 2, self.range_group, "t1", "时窗起点", -0.001)
        self.editor(h, 3, self.range_group, "t2", "时窗终点", 0.001)
        self.sliders = {}
        for idx, (key, title) in enumerate((("seconds_per_div", "时基"), ("center", "偏移"))):
            slider = ScopeSlider(title, "s", coarse=idx == 0)
            slider.committed.connect(lambda value, k=key: self.slider_edit(k, value))
            self.sliders[key] = slider
            h.addWidget(slider, 4 + idx, 0, 1, 2)
        self.sync_sliders(0.0002, 0)
        self.cell(v, 0, "显示档位", self.display.top, self.display.auto)
        self.cell(v, 1, "垂直中心", self.display.offset)
        for i, (key, title) in enumerate(zip(FRONTEND, ("输入阻抗", "输入耦合", "输入衰减", "模拟带宽")), 2):
            self.front(v, i, key, title)
        for i, slider in enumerate(self.display.sliders, 6):
            v.addWidget(slider, i, 0, 1, 2)
        for i, (key, title, default) in enumerate(
            (
                ("source", "触发源", "CURRENT"),
                ("mode", "触发模式", "AUTO"),
                ("edge", "边沿", "RISING"),
                ("level", "触发电平", 0),
            )
        ):
            self.editor(t, i, "scope_trigger", key, title, default)
        self.editor(a, 0, "scope_acquisition", "mode", "采集模式", "NORMAL")
        self.editor(a, 1, "scope_interpolation", "interpolation", "插值方式", "SINX")
        self.editor(a, 2, self.range_group, "max_length", "帧点数上限", 1024)
        self.settings = QLabel("等待首帧")
        self.settings.setObjectName("scopeSampleRate")
        self.settings.setMinimumHeight(self.settings.fontMetrics().lineSpacing() * 3 + 6)
        h.addWidget(self.settings, 6, 0, 1, 2)
        self.readback = QLabel()
        self.readback.setWordWrap(True)
        self.readback.setObjectName("rangeHint")
        v.addWidget(self.readback, 8, 0, 1, 2)

    def build_spectrum(self):
        self.range_group = "spectrum_span"
        self.endpoints = ("frequency1", "frequency2")
        self.alternate = ("center", "span")
        f = self.section("频率", self.column)
        for i, (key, label, default) in enumerate(
            (
                ("frequency1", "起始频率", 0),
                ("frequency2", "终止频率", 30000),
                ("center", "中心频率", 15000),
                ("span", "扫宽", 30000),
            )
        ):
            self.editor(f, i, self.range_group, key, label, default, FREQUENCY)
        a = self.section("幅度", self.column)
        self.cell(a, 0, "显示参考电平", self.display.top, self.display.auto)
        self.front(a, 1, "attenuation", "输入衰减")
        b = self.section("带宽", self.column)
        self.editor(b, 0, "spectrum_rbw", "mode", "RBW 模式", "AUTO")
        self.editor(b, 1, "spectrum_rbw", "rbw_value", "分辨率带宽", 100)
        self.editor(b, 2, "spectrum_window", "window", "窗函数", "HANN")
        self.editor(b, 3, "spectrum_averaging", "target_duration", "帧平均时长", 0.1)
        self.update_rbw()
        marker = QFrame()
        marker.setObjectName("scopeSection")
        marker_layout = QVBoxLayout(marker)
        marker_layout.setContentsMargins(15, 6, 15, 8)
        self.column.addWidget(marker)
        row = QHBoxLayout()
        marker_title = QLabel("Marker · 软件标记")
        marker_title.setObjectName("spectrumChartTitle")
        row.addWidget(marker_title)
        row.addStretch()
        self.add_marker = QPushButton("添加标记")
        self.add_marker.clicked.connect(self.page.add_center_marker)
        row.addWidget(self.add_marker)
        marker_layout.addLayout(row)
        self.page.marker_list = QListWidget()
        self.page.marker_list.setObjectName("spectrumMarkerList")
        self.page.marker_list.setFixedHeight(60)
        marker_layout.addWidget(self.page.marker_list)
        more = QPushButton("更多前端设置")
        more.setCheckable(True)
        self.column.addWidget(more)
        front = QWidget()
        self.column.addWidget(front)
        front.hide()
        more.toggled.connect(front.setVisible)
        layout = QVBoxLayout(front)
        layout.setContentsMargins(0, 0, 0, 0)
        extended = self.section("共享模拟前端", layout)
        for i, (key, title) in enumerate(
            (("impedance", "输入阻抗"), ("coupling", "输入耦合"), ("bandwidth", "模拟带宽"))
        ):
            self.front(extended, i, key, title)
        self.readback = QLabel()
        self.readback.setWordWrap(True)
        self.settings = QLabel()
        for i, readout in enumerate((self.readback, self.settings), 4):
            readout.setObjectName("rangeHint")
            extended.addWidget(readout, i, 0, 1, 2)

    def update_rbw(self):
        editors = self.editors["spectrum_rbw"]
        editors["rbw_value"].setEnabled(editors["mode"].input.currentData() == "MANUAL")

    def slider_edit(self, key, value):
        display_value(self.editors[self.range_group][key], value)
        self.edit(self.range_group, key)

    def sync_sliders(self, scale, center):
        # A quick adjustment around the current window. Numeric inputs keep the full device range.
        for key, value, low, high in (
            ("seconds_per_div", scale, scale / 100, scale * 100),
            ("center", center, center - 5 * scale, center + 5 * scale),
        ):
            slider = self.sliders[key]
            if not (isfinite(low) and isfinite(high) and low < high and scale >= 1e-300 and scale <= 1e300):
                slider.setEnabled(False)
                continue
            slider.setEnabled(True)
            slider.configure(value, Parameter(unit="s", minimum=low, maximum=high), scale)
            slider.slider.setToolTip("拖动调整草稿，点击「应用修改」发送到仪器。")

    def buttons(self):
        if hasattr(self, "apply_button"):
            modified = bool(self.dirty or any(box.currentData() for box in self.front_fields.values()))
            available = self.writable and self.active is None and self.reading is None
            self.apply_button.setEnabled(bool(available and modified))
            self.refresh_button.setEnabled(available)
            if modified:
                self.feedback.setText("待应用")

    def edit(self, group, key):
        self.dirty[group] = self.dirty.get(group, 0) + 1
        if group == self.range_group and key in self.endpoints + self.alternate:
            self.frequency_pair = self.endpoints if key in self.endpoints else self.alternate
            try:
                self.link_range()
            except (ValidationError, ValueError):
                pass  # Allow an unfinished numeric draft.
        if group == "spectrum_rbw":
            self.update_rbw()
        self.buttons()

    def link_range(self):
        editors = self.editors[self.range_group]
        pair = self.frequency_pair or self.endpoints
        a, b = (editors[k].draft() for k in pair)
        x, y = float(a["value"]) * a["factor"], float(b["value"]) * b["factor"]
        if not (isfinite(x) and isfinite(y)):
            raise ValidationError("请输入有限数值")
        if pair == self.endpoints:
            low, high = x, y
        elif self.scope:
            low, high = y - 5 * x, y + 5 * x
        else:
            low, high = x - y / 2, x + y / 2
        if not all(isfinite(v) for v in (low, high, high - low, (low + high) / 2)) or high <= low:
            raise ValidationError("终点必须大于起点")
        if not self.scope and not (0 <= low < high <= FREQUENCY.maximum):
            raise ValidationError("扫频端点须在 0 至 600 MHz 之间")
        if pair == self.endpoints:
            self.show_derived(low, high)
        else:
            for key, value in zip(self.endpoints, (low, high), strict=True):
                display_value(editors[key], value)
            if self.scope:
                self.sync_sliders(x, y)
        return dict(zip(self.endpoints, (low, high), strict=True))

    def show_derived(self, low, high):
        center, width = (low + high) / 2, high - low
        values = (width / 10, center) if self.scope else (center, width)
        for key, value in zip(self.alternate, values, strict=True):
            display_value(self.editors[self.range_group][key], value)
        if self.scope:
            self.sync_sliders(*values)

    def bind(self, session, channel):
        if self.context == (session, channel):
            return
        if self.active is not None:
            self.finish_group()
        self.context = (session, channel)
        self.dirty.clear()
        self.reading = None
        self.frequency_pair = None
        self.display.bind(channel)
        for (op, key), default in self.defaults.items():
            display_value(self.editors[op][key], default)
        for box in self.front_fields.values():
            box.setItemText(0, "保持当前")
            box.setCurrentIndex(0)
        if self.scope:
            self.sync_sliders(0.0002, 0)
        else:
            self.update_rbw()
        self.feedback.setText("等待读回")
        self.receipt.clear()
        self.buttons()

    def refresh(self):
        context = self.context
        if (
            self.active is not None
            or not self.writable
            or self.page.device_id in self.page.controller.group_devices
        ):
            return

        def read(_result=None):
            if context != self.context:
                return
            names = tuple(op for op in self.editors if CAPTURE[op][0] not in ACK_ONLY) + ("shared_frontend",)
            if self.scope:
                names += ("scope_samplerate",)
            self.reading = context
            self.buttons()
            try:
                future = self.page.controller.runtime.sessions.submit(
                    self.page.device_id, "read_settings", (names, self.page.channel)
                )
                self.page.controller.watch(future, received, failed)
            except Exception as exc:
                failed(exc)

        def failed(exc):
            if self.reading == context:
                self.reading = None
                self.receipt.setText(str(exc))
                self.buttons()

        def received(rows):
            if context != self.context or self.reading != context:
                return
            data = {}
            for row in rows:
                if not isinstance(row.value, dict):
                    failed(row.error or "当前参数读回不可用")
                    return
                if row.operation == "shared_frontend":
                    self.show_frontend(row.value)
                elif row.operation == "scope_samplerate":
                    data["samplerate"] = row.value
                else:
                    data[CAPTURE[row.operation][0]] = row.value
            self.reading = None
            self.sync(data)
            self.settings.setText(self.page.show_settings(data))
            self.receipt.setText("参数已刷新")
            self.buttons()

        if self.page._attached():
            read()
        else:
            self.page.command("attach_workspace", {}, "连接已有仪器槽并读取参数？", on_success=read)

    def finish_group(self):
        self.active = None
        self.page.controller.end_group(self.page.device_id, refresh=False)
        self.buttons()

    def sync(self, settings):
        for op, editors in self.editors.items():
            data = settings.get(CAPTURE[op][0])
            if not data or op in self.dirty:
                continue
            for key, editor in editors.items():
                value = field(data, key)
                if isinstance(value, str):
                    value = value.upper()
                    if key == "window" and value == "HANNING":
                        value = "HANN"
                if value is not None and not editor.hasFocus() and not editor.input.hasFocus():
                    display_value(editor, value)
            if op == self.range_group:
                low, high = (field(data, key) for key in self.endpoints)
                if all(type(n) in (int, float) for n in (low, high)):
                    self.show_derived(low, high)
            if op == "spectrum_rbw":
                self.update_rbw()
        if not self.dirty and not any(box.currentData() for box in self.front_fields.values()):
            self.feedback.setText("已同步")

    def show_frontend(self, read):
        for key, box in self.front_fields.items():
            value = read.get(key)
            box.setItemText(0, str(value) if value is not None else "保持当前")
        self.readback.setText("前端读回　" + " / ".join(str(read.get(k, "—")) for k in FRONTEND))

    def commands(self):
        result = []
        front = {key: box.currentData() for key, box in self.front_fields.items() if box.currentData()}
        if front:
            result.append(("shared_frontend", front))
        caps = self.page.controller.capabilities(self.page.device_id, self.page.channel)
        for op in self.dirty:
            editors = self.editors[op]
            if op == self.range_group:
                params = self.link_range()
                if self.scope:
                    params["max_length"] = editors["max_length"].value()
            else:
                params = {k: editor.value() for k, editor in editors.items() if editor.isEnabled()}
            wire_parameters(op, caps.get(op).validate(params))
            if op == "spectrum_span" and params["frequency2"] > 300e6 and front.get("bandwidth") == "300MHz":
                raise ValidationError("当前扫频范围需要 600 MHz 模拟带宽")
            result.append((op, params))
        return result

    def apply(self):
        if (
            self.active is not None
            or self.reading is not None
            or not self.writable
            or self.page.device_id in self.page.controller.group_devices
        ):
            return
        try:
            queue = self.commands()
        except (ValueError, ValidationError) as exc:
            self.receipt.setText(str(exc))
            return
        if not queue or not self.page._confirm(
            "应用这些修改？测量参数作用于当前仪器槽，前端设置由同一物理输入共用。"
        ):
            return
        try:
            self.page.controller.begin_group(self.page.device_id)
        except Exception as exc:
            self.receipt.setText(str(exc))
            return
        ticket = self.active = object()
        versions = self.dirty.copy()
        self.buttons()
        done: list[str] = []
        channel = self.page.channel

        def failed(reason):
            if self.active is ticket:
                self.finish_group()
                self.receipt.setText(f"已应用 {len(done)} 组。{reason}")
                self.buttons()

        def submit():
            if self.active is not ticket:
                return
            live: dict = next(
                (
                    s
                    for s in self.page.controller.runtime.sessions.snapshots()
                    if s["stableDeviceId"] == self.page.device_id
                ),
                {},
            )
            if (live.get("sessionId"), self.page.channel) != self.context:
                failed("会话或通道已切换")
                return
            op, params = queue.pop(0)

            def accepted(result):
                if self.active is not ticket:
                    return
                if result.outcome != "success":
                    failed(result.error)
                    return
                if op == "shared_frontend":
                    self.show_frontend(result.readback)
                    for key, value in params.items():
                        if self.front_fields[key].currentData() == value:
                            self.front_fields[key].setCurrentIndex(0)
                    done.append("前端已读回")
                else:
                    method, specs = CAPTURE[op]
                    actual = result.readback
                    if self.dirty.get(op) == versions[op]:
                        self.dirty.pop(op)
                    self.sync({method: actual})
                    if op == "scope_trigger":
                        self.page.show_trigger(actual, channel if params["source"] == "CURRENT" else None)
                    values = [
                        engineering(v, specs[k].unit) if k in specs and specs[k].unit else str(v)
                        for k, v in actual.items()
                    ]
                    done.append(
                        ("设置回执" if method in ACK_ONLY else "设备读回") + " · " + " / ".join(values)
                    )
                if queue:
                    submit()
                else:
                    self.finish_group()
                    self.receipt.setText(done[-1] if len(done) == 1 else f"已应用 {len(done)} 组修改")
                    self.receipt.setToolTip("\n".join(done))
                    self.feedback.setText("已应用")
                    self.buttons()

            try:
                self.page.controller.apply(
                    self.page.device_id,
                    DeviceCommand(op, params, channel, confirmed=True),
                    accepted,
                    failed,
                    group_member=True,
                )
            except Exception as exc:
                failed(exc)

        submit()
