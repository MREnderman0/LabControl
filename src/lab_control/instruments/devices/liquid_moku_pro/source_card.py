from PySide6.QtWidgets import QGridLayout, QLabel

from lab_control.ui.kit import Card, button
from lab_control.ui.pages.device_control.editors import ParameterEditor


class GeneratorCard(Card):
    """One physical output, with independent drafts and an explicit load confirmation."""

    def __init__(self, page, channel):
        super().__init__(f"CH {channel}", padding=(16, 12, 16, 14))
        self.page, self.channel = page, channel
        self.header().addStretch()
        self.add_status("正弦 · 零偏置", dot=False)
        self.fields = fields = QGridLayout()
        self.body.addLayout(fields)
        fields.setHorizontalSpacing(12)
        cap = page.controller.capabilities(page.device_id, channel).get("standard_waveform")
        self.editors = {}
        self.captions = []
        for col, (name, label, default) in enumerate(
            (("frequency", "频率", 10000.0), ("amplitude", "幅度", 0.1))
        ):
            caption = QLabel(label)
            caption.setObjectName("controlTitle")
            editor = ParameterEditor(cap.parameters[name], simplified=True, label=label)
            editor.set_value(default)
            self.editors[name] = editor
            self.captions.append(caption)
            fields.addWidget(caption, 0, col)
            fields.addWidget(editor, 1, col)
            fields.setColumnStretch(col, 1)
        self.on = button("开启输出", "primary", "play")
        self.off = button("关闭输出", "", "stop")
        self.off.setToolTip(f"关闭 CH {channel} 输出")
        self.on.clicked.connect(self.start)
        self.off.clicked.connect(self.stop)
        footer = self.footer()
        footer.addStretch()
        footer.addWidget(self.on)
        footer.addWidget(self.off)
        self.compact = None

    def resizeEvent(self, event):
        super().resizeEvent(event)
        compact = self.width() < 560
        if compact == self.compact:
            return
        self.compact = compact
        for index, (caption, editor) in enumerate(zip(self.captions, self.editors.values(), strict=True)):
            col = 0 if compact else index
            row = 2 * index if compact else 0
            span = 2 if compact else 1
            self.fields.addWidget(caption, row, col, 1, span)
            self.fields.addWidget(editor, row + 1, col, 1, span)

    def start(self):
        try:
            values = {name: editor.value() for name, editor in self.editors.items()}
            self.page.command(
                "standard_waveform",
                values,
                f"确认 CH {self.channel} 实际负载为 50 Ω，输出 {values['frequency']:g} Hz、"
                f"{1000 * values['amplitude']:g} mVpp、零偏置正弦波？",
                channel=self.channel,
            )
        except Exception as exc:
            self.page.status.setText(str(exc))

    def stop(self):
        self.page.command("waveform_stop", {}, f"关闭信号源 CH {self.channel}？", channel=self.channel)
