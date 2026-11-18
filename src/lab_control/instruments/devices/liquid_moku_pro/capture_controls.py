"""Graphite acquisition parameter editors shared by the two Moku measurement pages."""

from PySide6.QtWidgets import QFormLayout, QLabel, QStackedWidget, QVBoxLayout, QWidget

from lab_control.domain.oscilloscope import engineering
from lab_control.ui.pages.device_control.editors import UNIT_CHOICES, ParameterEditor
from lab_control.ui.widgets.workspace_combo import WorkspaceComboBox

from .capture_settings import ACK_ONLY, CAPTURE, FREQUENCY, TOKENS, field

GROUPS = {
    "scope_timebase": (
        "时基",
        (("t1", "时窗起点", -0.001), ("t2", "时窗终点", 0.001), ("max_length", "点数上限", 1024)),
    ),
    "scope_trigger": (
        "边沿触发",
        (
            ("source", "触发源", "CURRENT"),
            ("mode", "触发模式", "AUTO"),
            ("edge", "触发边沿", "RISING"),
            ("level", "触发电平", 0),
        ),
    ),
    "scope_acquisition": ("采集模式", (("mode", "采集模式", "NORMAL"),)),
    "scope_interpolation": ("波形插值", (("interpolation", "插值方式", "SINX"),)),
    "spectrum_span": ("起止频率", (("frequency1", "起始频率", 0), ("frequency2", "终止频率", 30000))),
    "spectrum_center": ("中心频率与跨度", (("center", "中心频率", 15000), ("span", "频率跨度", 30000))),
    "spectrum_rbw": ("分辨率带宽", (("mode", "RBW 模式", "AUTO"), ("rbw_value", "手动 RBW", 100))),
    "spectrum_window": ("窗函数", (("window", "窗函数", "HANN"),)),
    "spectrum_averaging": ("帧平均", (("target_duration", "目标帧时长", 0.1),)),
}


def operation(group):
    return "spectrum_span" if group == "spectrum_center" else group


def display_value(editor, value):
    scales = UNIT_CHOICES.get(editor.spec.unit)
    if scales and type(value) in (int, float) and not editor.spec.choices:
        factor = editor.unit.currentData() or 1
        if not editor.unit_selected and value:
            factor = max((scale for _, scale in scales if scale <= abs(value)), default=scales[0][1])
        editor.restore({"value": f"{value / factor:.12g}", "factor": factor}, readback=True)
    else:
        editor.set_value(value, readback=True)


class CaptureControls(QWidget):
    def __init__(self, page):
        super().__init__()
        self.page = page
        self.groups = WorkspaceComboBox()
        self.forms = QStackedWidget()
        self.editors = {}
        self.dirty = {}
        self.seen = {}
        self.receipts = {}
        self.session = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.groups)
        layout.addWidget(self.forms)
        prefix = "scope_" if page.kind == "waveform" else "spectrum_"
        for group, (label, fields) in GROUPS.items():
            if not group.startswith(prefix):
                continue
            self.groups.addItem(label, group)
            box = QWidget()
            form = QFormLayout(box)
            form.setContentsMargins(0, 4, 0, 0)
            self.forms.addWidget(box)
            self.editors[group] = {}
            for key, title, default in fields:
                spec = FREQUENCY if group == "spectrum_center" else CAPTURE[group][1][key]
                editor = ParameterEditor(spec, simplified=True, label=title)
                if spec.kind == "enum":
                    for index, token in enumerate(spec.choices):
                        editor.input.setItemText(
                            index,
                            {"CURRENT": "当前物理通道", "EXTERNAL": "外部触发"}.get(token, TOKENS[token]),
                        )
                editor.range_hidden = True
                if key == "max_length":
                    editor.unit.hide()
                display_value(editor, default)
                editor.changed.connect(lambda g=group: self.mark_edited(g))
                self.editors[group][key] = editor
                form.addRow(title, editor)
            if operation(group) == "spectrum_span":
                limit = QLabel("超过 300 MHz 时，所选输入需启用 600 MHz 模拟带宽。")
                limit.setObjectName("rangeHint")
                limit.setWordWrap(True)
                form.addRow(limit)
        if "spectrum_rbw" in self.editors:
            self.editors["spectrum_rbw"]["mode"].changed.connect(self.update_rbw)
            self.update_rbw()
        self.apply_button = page._button(layout, "应用参数", self.apply, variant="primary")
        self.feedback = QLabel()
        self.feedback.setWordWrap(True)
        self.feedback.setObjectName("rangeHint")
        layout.addWidget(self.feedback)
        self.groups.currentIndexChanged.connect(self.select)
        self.select()

    def select(self):
        self.forms.setCurrentIndex(self.groups.currentIndex())
        self.feedback.setText(self.receipts.get(operation(self.groups.currentData()), ""))

    def mark_edited(self, group):
        self.dirty[group] = self.dirty.get(group, 0) + 1

    def update_rbw(self):
        editors = self.editors["spectrum_rbw"]
        editors["rbw_value"].setEnabled(editors["mode"].input.currentData() == "MANUAL")

    def reset_session(self, session):
        if self.session != session:
            self.session = session
            self.receipts.clear()
            self.seen.clear()
            self.dirty.clear()
            for group, editors in self.editors.items():
                for key, _, default in GROUPS[group][1]:
                    display_value(editors[key], default)
            if "spectrum_rbw" in self.editors:
                self.update_rbw()
            self.select()

    def sync(self, settings):
        for group, editors in self.editors.items():
            method, _ = CAPTURE[operation(group)]
            data = settings.get(method)
            if not data or group in self.dirty or data == self.seen.get(group):
                continue
            self.seen[group] = data.copy()
            if group == "spectrum_center":
                low, high = field(data, "frequency1"), field(data, "frequency2")
                if not all(type(n) in (float, int) for n in (low, high)):
                    continue
                data = {"center": (low + high) / 2, "span": high - low}
            for key, editor in editors.items():
                val = field(data, key)
                if isinstance(val, str):
                    val = val.upper()
                    if key == "window" and val == "HANNING":
                        val = "HANN"
                if val is not None:
                    display_value(editor, val)
            if group == "spectrum_rbw":
                self.update_rbw()

    def apply(self):
        group = self.groups.currentData()
        op = operation(group)
        fields = self.editors[group]
        params = {key: editor.value() for key, editor in fields.items() if editor.isEnabled()}
        if group == "spectrum_center":
            center, half = params["center"], params["span"] / 2
            params = {"frequency1": center - half, "frequency2": center + half}
        label = GROUPS[group][0]
        ch, session = self.page.channel, self.session
        revision = self.dirty.get(group, 0)

        def completed(result):
            if self.session != session:
                return
            method, specs = CAPTURE[op]
            actual = result.readback
            parts = []
            for key, value in actual.items():
                spec = specs.get(key)
                parts.append(engineering(value, spec.unit) if spec and spec.unit else str(value))
            prefix = "设置回执" if method in ACK_ONLY else "设备读回"
            self.receipts[op] = f"{prefix} · " + " / ".join(parts)
            if op == "scope_trigger" and params["source"] == "CURRENT":
                self.receipts[op] += f"（CH {ch}）"
            if self.dirty.get(group, 0) == revision:
                self.dirty.pop(group, None)
                self.seen.pop(group, None)
            self.sync({method: actual})
            self.select()

        target = f"CH {ch}" if op == "scope_trigger" else "当前仪器槽的所有通道"
        self.page.command(op, params, f"将{label}设置应用到{target}？", on_success=completed)
