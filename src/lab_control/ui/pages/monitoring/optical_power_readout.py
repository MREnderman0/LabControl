"""Compact optical-power rows sharing column geometry with their fixed header."""

from __future__ import annotations

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QCheckBox, QFrame, QHBoxLayout, QLabel, QSizePolicy, QWidget

from lab_control.application.power_window import PowerChannelSnapshot
from lab_control.domain.power import power_span, watts_to_dbm
from lab_control.ui.kit.base import ElideLabel
from lab_control.ui.kit.numeric import ReadingButton, ReadingLabel, number_text
from lab_control.ui.pages.monitoring.readout_columns import add_readout_column
from lab_control.ui.styles import tokens


def set_columns(widget: QWidget, fields: tuple[QWidget, ...]) -> None:
    layout = QHBoxLayout(widget)
    layout.setContentsMargins(4, 0, 4, 0)
    layout.setSpacing(8)
    for field, (width, stretch) in zip(fields, ((28, 0), (44, 22), (86, 40), (86, 38)), strict=True):
        add_readout_column(layout, field, width, stretch)


class ColumnGrip(QWidget):
    def __init__(self, header, column):
        super().__init__(header)
        self.header = header
        self.column = column
        self.setCursor(Qt.CursorShape.SplitHCursor)
        self.setToolTip("拖动调整相邻列宽")

    def paintEvent(self, event):  # noqa: N802
        painter = QPainter(self)
        painter.setPen(QColor(tokens.BORDER_HOVER if self.underMouse() else tokens.BORDER_STRONG))
        x = self.width() // 2
        painter.drawLine(x, 6, x, self.height() - 6)

    def mousePressEvent(self, event):  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.start = event.globalPosition().x()
            self.widths = [w.width() for w in self.header.labels[1:]]
            event.accept()

    def mouseMoveEvent(self, event):  # noqa: N802
        if event.buttons() & Qt.MouseButton.LeftButton and hasattr(self, "widths"):
            i = self.column
            widths = self.widths.copy()
            left, right = self.header.labels[i + 1 : i + 3]
            delta = round(event.globalPosition().x() - self.start)
            delta = max(left.minimumWidth() - widths[i], min(delta, widths[i + 1] - right.minimumWidth()))
            widths[i] += delta
            widths[i + 1] -= delta
            self.header.apply_widths(widths)
            event.accept()

    def mouseReleaseEvent(self, event):  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and hasattr(self, "widths"):
            del self.widths
            self.header.committed.emit(self.header.weights)
            event.accept()


class ReadoutHeader(QWidget):
    labels: list[QLabel]
    grips: list[ColumnGrip]
    changed = Signal(list)
    committed = Signal(list)

    def apply_widths(self, weights):
        if (
            isinstance(weights, list)
            and len(weights) == 3
            and all(type(x) is int and 0 < x < 100000 for x in weights)
        ):
            self.weights = list(weights)
            layout = self.layout()
            assert isinstance(layout, QHBoxLayout)
            for i, weight in enumerate(weights, 1):
                layout.setStretch(i, weight)
            layout.activate()
            self.changed.emit(self.weights)
            self.position_grips()

    def position_grips(self):
        for grip, label in zip(self.grips, self.labels[1:3], strict=True):
            grip.setGeometry(label.geometry().right() + 1, 0, 8, self.height())
            grip.raise_()

    def event(self, event):
        result = super().event(event)
        if hasattr(self, "grips") and event.type() in (
            QEvent.Type.Resize,
            QEvent.Type.LayoutRequest,
            QEvent.Type.Show,
        ):
            self.position_grips()
        return result


def readout_header(parent=None) -> ReadoutHeader:
    header = ReadoutHeader(parent)
    header.setObjectName("powerReadoutHeader")
    fields = []
    for column, text in enumerate(("监测", "通道", "功率", "峰峰值 · 30 min")):
        label = QLabel(text)
        label.setObjectName("powerReadoutHeading")
        label.setAlignment(
            Qt.AlignmentFlag.AlignVCenter
            | (Qt.AlignmentFlag.AlignRight if column >= 2 else Qt.AlignmentFlag.AlignLeft)
        )
        fields.append(label)
    set_columns(header, tuple(fields))
    header.labels = fields
    header.weights = [22, 40, 38]
    header.grips = [ColumnGrip(header, i) for i in range(2)]
    header.setFixedHeight(26)
    return header


class PowerMeterReadoutRow(QFrame):
    def __init__(self, channel: int, parent=None) -> None:
        super().__init__(parent)
        self.channel = channel
        self.selected = True
        self._history: PowerChannelSnapshot | None = None
        self._unit = "W"
        self.setObjectName("powerMeterReadoutRow")
        self.setFixedHeight(28)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.check = QCheckBox()
        self.check.setAccessibleName(f"CH {channel} 参与监测")
        self.check.setToolTip("参与此通道的监测与记录；取消后保留最近 30 分钟历史")
        self.channel_label = ElideLabel(f"CH {channel}")
        self.channel_label.setObjectName("powerReadoutChannel")
        self.channel_label.setProperty("channel", str(((channel - 1) % 8) + 1))
        self.value = ReadingButton("— W")
        self.value.setObjectName("powerReadoutValue")
        self.value.setCursor(Qt.CursorShape.PointingHandCursor)
        self.value.setAccessibleName(f"CH {channel} 功率；点击切换全部读数和曲线单位")
        self.peak = ReadingLabel("— W")
        self.peak.setObjectName("powerReadoutPeak")
        self.peak.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        set_columns(self, (self.check, self.channel_label, self.value, self.peak))
        self.set_selected(True)

    def set_selected(self, selected: bool) -> None:
        if self.property("participating") == selected:
            return
        self.selected = selected
        self.setProperty("participating", selected)
        for widget in (self, self.channel_label, self.value, self.peak):
            widget.style().unpolish(widget)
            widget.style().polish(widget)
            widget.update()
        self.update_window(self._history, self._unit)

    def update_window(self, history: PowerChannelSnapshot | None, unit: str) -> None:
        self._history, self._unit = history, unit
        peak_unit = "W" if unit == "W" else "dB"
        if not self.selected:
            self.value.setText("未监测")
            self.peak.setText("—")
            self.value.setToolTip("未监测 · 历史按最近 30 分钟保留\n点击切换全部读数及曲线的 W / dBm 显示")
            self.peak.setToolTip("重新勾选后继续使用仍在窗口内的有效样本")
            return
        watts = history.points[-1].watts if history and history.points else None
        value = watts if unit == "W" else watts_to_dbm(watts) if watts is not None else None
        span = power_span(history.minimum, history.maximum, unit) if history else None
        self.value.setText(self.format_value(value, unit))
        self.peak.setText(self.format_value(span, peak_unit))
        updated = (
            history.timestamp.replace("T", " ")
            if history and history.timestamp
            else "最近 30 分钟内无有效数据"
            if history
            else "尚未采集"
        )
        raw = (
            f"\n原始 {history.raw_value:.17g} {history.raw_unit}"
            if history and history.raw_value is not None
            else ""
        )
        full = f"{value:.17g} {unit}" if value is not None else f"— {unit}"
        self.value.setToolTip(f"{full}{raw}\n更新时间：{updated}\n点击同步切换全部读数和曲线：W / dBm")
        formula = "最大功率 − 最小功率" if unit == "W" else "10 × log10(最大功率 / 最小功率)"
        peak_full = f"{span:.17g} {peak_unit}" if span is not None else f"— {peak_unit}"
        count = len(history.points) if history else 0
        self.peak.setToolTip(
            f"{peak_full}\n{formula}\n最近 30 分钟内的 {count} 个有效样本；不足 30 分钟使用本轮已有数据"
        )

    @staticmethod
    def format_value(value: float | None, unit: str) -> str:
        if value is None:
            return f"— {unit}"
        return f"{number_text(value, 0 if unit == 'W' else 3)} {unit}"
