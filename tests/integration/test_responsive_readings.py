from decimal import Decimal

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from lab_control.app import load_stylesheet, ui_font
from lab_control.ui.kit.numeric import ReadingButton, ReadingLabel, fit_reading
from lab_control.ui.pages.monitoring.optical_power_readout import readout_header


@pytest.mark.parametrize("kind", [ReadingLabel, ReadingButton])
@pytest.mark.parametrize(
    "text",
    [
        "-4.623551234567891e-06 W",
        "0.00230834123456789 W",
        "-999999999999999.875 Hz",
        "7.234567890123456e+108 Hz",
    ],
)
def test_resize_never_loses_magnitude_and_widening_restores_source(qapp, qtbot, kind, text):
    qapp.setFont(ui_font())
    qapp.setStyleSheet(load_stylesheet())
    field = kind(text)
    field.setObjectName("powerReadoutValue" if kind is ReadingButton else "powerReadoutPeak")
    qtbot.addWidget(field)
    field.show()
    original = Decimal(text.split()[0])
    for width in (480, 138, 100, 82, 148, 480):
        field.resize(width, 30)
        qtbot.wait(2)
        number, unit = field.text().split()
        rounded = Decimal(number)
        assert abs(rounded / original - 1) <= Decimal("0.09")
        assert unit == text.split()[1] and field.toolTip() == text
        assert field.fontMetrics().horizontalAdvance(field.text()) <= width
        if "e" in text:
            assert "e" in number
    assert field.text() == text


def test_rounding_carries_into_integer_or_exponent(qtbot):
    label = ReadingLabel()
    qtbot.addWidget(label)
    metrics = label.fontMetrics()
    for full, expected in [("9.99987 W", "10.0 W"), ("9.9999e-06 W", "1.0e-05 W")]:
        assert fit_reading(full, metrics.horizontalAdvance(expected), metrics) == expected


def test_drag_commits_ratios_and_reopens_at_different_width(qtbot):
    header = readout_header()
    qtbot.addWidget(header)
    header.resize(520, 26)
    header.show()
    qtbot.wait(10)
    for i, grip in enumerate(header.grips, 1):
        pos = grip.rect().center()
        start = header.labels[i].width()
        with qtbot.waitSignal(header.committed) as signal:
            QTest.mousePress(grip, Qt.MouseButton.LeftButton, pos=pos)
            QTest.mouseMove(grip, pos + type(pos)(25, 0))
            QTest.mouseRelease(grip, Qt.MouseButton.LeftButton, pos=pos)
        assert header.labels[i].width() > start
    restored = readout_header()
    qtbot.addWidget(restored)
    restored.apply_widths(signal.args[0])
    restored.resize(700, 26)
    restored.show()
    qtbot.wait(10)
    assert restored.weights == header.weights
    assert restored.labels[0].width() == header.labels[0].width()
    assert restored.labels[2].width() > header.labels[2].width()
