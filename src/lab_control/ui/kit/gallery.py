"""Component gallery: every kit component in its states, on the runtime theme.

    python -m lab_control.ui.kit.gallery            # window
    python -m lab_control.ui.kit.gallery out.png    # render to an image and exit

Uses no devices and writes nothing except the optional image.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import (
    QApplication,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from lab_control.ui.kit import (
    Card,
    DeviceChips,
    Dot,
    EmptyState,
    FieldBox,
    Hint,
    IconButton,
    IconTile,
    NavButton,
    Pill,
    ReadoutStrip,
    SearchBox,
    Section,
    Segmented,
    SubNavButton,
    Switch,
    button,
    label,
    paint_device_card,
)
from lab_control.ui.styles import tokens as tk
from lab_control.ui.widgets.workspace_combo import WorkspaceComboBox


class _DeviceCardDemo(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setFixedSize(360, 54)

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        paint_device_card(
            painter,
            QRect(0, 0, self.width(), self.height()),
            icon_name="spec",
            name="DSA815",
            subtitle="RIGOL · DSA8A245001754 · 槽位未绑定",
            state="已连接",
            kind="ok",
            font=self.font(),
        )


def _row(*widgets: QWidget, stretch: bool = True) -> QHBoxLayout:
    row = QHBoxLayout()
    row.setSpacing(8)
    for widget in widgets:
        row.addWidget(widget)
    if stretch:
        row.addStretch(1)
    return row


def _field(value: str, unit: str | None, *, state: str = "") -> FieldBox:
    box = FieldBox()
    edit = QLineEdit(value)
    edit.setObjectName("sourceValue")
    box.add(edit, 1)
    if unit:
        combo = WorkspaceComboBox()
        combo.setProperty("fieldUnit", True)
        combo.addItems([unit, "kHz", "Hz"])
        combo.fit_contents(56)
        box.add(combo)
    if state:
        box.set_state(state, True)
    box.setFixedWidth(200)
    return box


def build() -> QWidget:
    page = QWidget()
    page.setObjectName("cardWorkspace")
    column = QVBoxLayout(page)
    column.setContentsMargins(24, 20, 24, 24)
    column.setSpacing(16)
    title = QLabel("LabControl 组件库")
    title.setObjectName("pageTitle")
    column.addWidget(title)
    note = QLabel("ui/kit · 颜色取自 tokens.py，尺寸与状态在 card_workspace.qss 的 Component kit 一节")
    note.setObjectName("pageDescription")
    column.addWidget(note)
    grid = QGridLayout()
    grid.setSpacing(16)
    column.addLayout(grid)

    buttons = Card("按钮")
    buttons.body.addLayout(
        _row(
            button("扫描设备", "primary", "scan"),
            button("一键连接", "", "plug"),
            button("地址列表", "ghost", "network"),
        )
    )
    buttons.body.addLayout(
        _row(
            button("停止记录", "danger", "stop"),
            button("添加", "ghost", "plus", "sm"),
            button("查看全部", "link"),
        )
    )
    disabled = button("应用修改", "primary")
    disabled.setEnabled(False)
    buttons.body.addLayout(
        _row(
            disabled,
            IconButton("updown", "恢复视图"),
            IconButton("download", "保存当前帧"),
            IconButton("pin", "固定纵轴"),
            IconButton("more", "更多曲线操作"),
            IconButton("bell", "告警", badge=True),
        )
    )
    grid.addWidget(buttons, 0, 0)

    status = Card("状态与排版")
    status.body.addLayout(
        _row(
            Pill("已连接", "ok", dot=True),
            Pill("采集中", "accent", dot=True),
            Pill("采集中 · 参数已锁定", "warn", icon="lock"),
        )
    )
    live = Pill("记录中", "err", dot=True)
    live.set_kind("err", live=True)
    status.body.addLayout(_row(live, Pill("未连接", "neutral", dot=True), Pill("1 项", "err")))
    status.body.addLayout(
        _row(
            Dot("ok", 8, live=True),
            Dot("warn", 8),
            Dot("err", 8),
            Dot("idle", 8),
            *(Dot(tk.channel_color(ch), 8) for ch in range(1, 5)),
            label("1.000 MHz", "mono"),
            label("−30.09 dBm", "value"),
        )
    )
    grid.addWidget(status, 0, 1)

    inputs = Card("输入")
    inputs.body.addLayout(_row(_field("1.000", "MHz"), _field("2", "MHz", state="dirty")))
    inputs.body.addLayout(_row(_field("-10", "dBm", state="error"), _field("0.25", None)))
    on = Switch("自动")
    on.setChecked(True)
    inputs.body.addLayout(_row(Switch("通道开启"), on))
    seg = Segmented([("水平", 0), ("垂直", 1), ("触发", 2), ("采集", 3)])
    inputs.body.addLayout(_row(seg))
    grid.addWidget(inputs, 1, 0)

    device = Card("仪器")
    device.body.addWidget(_DeviceCardDemo())
    chips = DeviceChips()
    chips.set_devices(
        [
            ("信号源", "generator", "ok", ""),
            ("示波器", "scope", "ok", ""),
            ("频谱仪", "spectrum", "ok", ""),
        ],
        "spectrum",
    )
    chips.setMinimumWidth(chips.sizeHint().width())
    device.body.addLayout(_row(chips))
    strip = ReadoutStrip(
        {"center": "中心频率", "span": "扫宽", "ref": "参考电平", "marker": "M1 峰值"}, highlight=("marker",)
    )
    for key, value in (
        ("center", "1.000 MHz"),
        ("span", "2.000 MHz"),
        ("ref", "−10 dBm"),
        ("marker", "−30.09 dBm"),
    ):
        strip.set(key, value)
    device.body.addWidget(strip)
    tiles = QHBoxLayout()
    for name in ("spec", "scope", "zap", "sine", "target", "thermo"):
        tiles.addWidget(IconTile(name, 34))
    tiles.addStretch(1)
    device.body.addLayout(tiles)
    grid.addWidget(device, 1, 1)

    nav = Card("导航与搜索", padding=(10, 12, 10, 12), spacing=2)
    home = NavButton("首页", "home")
    instruments = NavButton("仪器测控", "sliders")
    instruments.set_parent_active(True)
    nav.body.addWidget(home)
    nav.body.addWidget(instruments)
    for nav_label, count, checked in (("频谱仪", 5, True), ("示波器", 3, False), ("电源", 3, False)):
        item = SubNavButton(nav_label, count)
        item.setChecked(checked)
        nav.body.addWidget(item)
    watch = NavButton("监视列表", "eye")
    watch.set_badge(1)
    nav.body.addWidget(watch)
    nav.body.addSpacing(8)
    nav.body.addWidget(SearchBox("搜索设备、型号或功能槽位", shortcut=""))
    grid.addWidget(nav, 2, 0)

    panel = Card("参数", padding=0, spacing=0)
    panel.header().addWidget(Pill("已同步", "ok", dot=True))
    panel.header().addStretch(1)
    panel.header().addWidget(button("刷新参数", "ghost", "refresh", "sm"))
    section = Section("频率")
    section.layout_.addLayout(_row(_field("0", "Hz"), _field("2", "MHz")))
    panel.body.addWidget(section)
    last = Section("提示", last=True)
    last.layout_.addWidget(Hint("拖动预览 · 松手写入仪器 · 也可直接拖动图上的 0 V 与 T 标签"))
    panel.body.addWidget(last)
    foot = panel.footer()
    foot.addWidget(button("自动设置", "", "wand"), 1)
    foot.addWidget(button("应用修改", "primary"), 1)
    grid.addWidget(panel, 2, 1)

    empty = EmptyState("还没有监视预设", "点击“新建”，把需要盯着的通道放进一个预设", "inbox")
    empty.action_row.addWidget(button("新建预设", "primary", "plus"))
    column.addWidget(empty)
    column.addStretch(1)
    return page


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    from lab_control.app import load_stylesheet, load_ui_font, ui_font

    existing = QApplication.instance()
    app = existing if isinstance(existing, QApplication) else QApplication(sys.argv[:1])
    app.setStyle("Fusion")
    load_ui_font()
    app.setFont(ui_font())
    app.setStyleSheet(load_stylesheet())
    scroll = QScrollArea()
    scroll.setWindowTitle("LabControl · 组件库")
    scroll.setWidgetResizable(True)
    scroll.setWidget(build())
    scroll.resize(1180, 1040)
    if argv:
        scroll.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        scroll.show()
        for _ in range(5):
            app.processEvents()
        if (content := scroll.widget()) is not None:
            content.grab().save(str(Path(argv[0])))
        return 0
    scroll.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["build", "main"]
