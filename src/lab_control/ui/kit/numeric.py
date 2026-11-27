"""Width-aware formatting for display-only instrument readings."""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal, localcontext

from PySide6.QtCore import QEvent, QObject, QSize, Qt
from PySide6.QtWidgets import QLabel, QPushButton, QStyle, QStyledItemDelegate, QStyleOptionButton

# Match magnitudes, including grouped digits, without treating CH1 or M2 as readings.
_NUMBER = re.compile(r"(?<![\w.])[-+−]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:[eE][-+−]?\d+)?(?![\d.])")


def number_text(value, min_decimals=0):
    if value is None:
        return "—"
    text = str(value).removesuffix(".0")
    if min_decimals and "e" not in text.lower():
        head, _, tail = text.partition(".")
        return head + "." + tail.ljust(min_decimals, "0")
    return text


def fit_reading(text, width, metrics):
    """Round whole numeric tokens, preserving their sign, unit and magnitude."""
    measure = metrics.horizontalAdvance
    if measure(text) <= width:
        return text
    matches = list(_NUMBER.finditer(text))
    if not matches:
        return metrics.elidedText(text, Qt.TextElideMode.ElideRight, max(0, width))
    values = [Decimal(m[0].replace(",", "").replace("−", "-")) for m in matches]
    # Retain ordinary integers intact until scientific notation is necessary.
    with localcontext() as ctx:
        ctx.rounding = ROUND_HALF_UP
        for scientific in (False, True):
            for precision in range(min(17, max(len(v.as_tuple().digits) for v in values)), 0, -1):
                pieces: list[str] = []
                start = 0
                for match, value in zip(matches, values, strict=True):
                    original = match[0]
                    if scientific or "e" in original.lower():
                        mantissa, exponent = format(value, f".{precision - 1}e").split("e")
                        # Decimal formats zero with a precision-dependent exponent.
                        exponent_value = int(exponent) if value else 0
                        token = f"{mantissa}e{exponent_value:+03d}"
                    else:
                        places = max(
                            0, min(-int(value.as_tuple().exponent), precision - value.adjusted() - 1)
                        )
                        token = format(value, f".{places}f")
                    if original.startswith("+"):
                        token = "+" + token
                    if "−" in original:
                        token = token.replace("-", "−")
                    pieces.extend((text[start : match.start()], token))
                    start = match.end()
                shown = "".join(pieces) + text[start:]
                if measure(shown) <= width:
                    return shown
                if scientific:
                    compact = re.sub(r"e([+−-])0*(\d+)", lambda m: "e" + m[1].lstrip("+") + m[2], shown)
                    if measure(compact) <= width:
                        return compact
    # A partial sign/exponent would have a different physical meaning.
    return "…" if width >= measure("…") else ""


class _FitOnResize(QObject):
    def eventFilter(self, widget, event):  # noqa: N802
        if event.type() in (
            QEvent.Type.Resize,
            QEvent.Type.FontChange,
            QEvent.Type.StyleChange,
            QEvent.Type.Show,
        ):
            fit_widget(widget)
        return False


def fit_widget(widget):
    rect = widget.contentsRect()
    if isinstance(widget, QPushButton):
        option = QStyleOptionButton()
        widget.initStyleOption(option)
        rect = widget.style().subElementRect(QStyle.SubElement.SE_PushButtonContents, option, widget)
    shown = fit_reading(widget.full_text, max(0, rect.width() - 2), widget.fontMetrics())
    if widget.text() != shown:
        if isinstance(widget, QPushButton):
            QPushButton.setText(widget, shown)
        else:
            QLabel.setText(widget, shown)


class ReadingLabel(QLabel):
    def __init__(self, text="", parent=None):
        super().__init__(parent)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setText(text)
        self.installEventFilter(_FitOnResize(self))

    def setText(self, text):  # noqa: N802
        self.full_text = text
        fit_widget(self)
        self.setToolTip(text)

    def minimumSizeHint(self):  # noqa: N802
        return QSize(0, super().minimumSizeHint().height())


class ReadingButton(QPushButton):
    def __init__(self, text="", parent=None):
        super().__init__(parent)
        self.setText(text)
        self.installEventFilter(_FitOnResize(self))

    def setText(self, text):  # noqa: N802
        self.full_text = text
        fit_widget(self)
        self.setToolTip(text)


class ReadingDelegate(QStyledItemDelegate):
    """Fit display text only. Editors and model roles retain their source values."""

    def initStyleOption(self, option, index):  # noqa: N802
        super().initStyleOption(option, index)
        if option.rect.width() > 0:
            option.text = fit_reading(option.text, option.rect.width() - 10, option.fontMetrics)
