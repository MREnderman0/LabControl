"""Graphite page frame shared by the monitoring pages.

Pages keep building their own widgets (signals and refresh logic stay where they are); these
helpers only arrange them like the spectrum and oscilloscope pages: a header row with the device
card and run controls, and chart cards with a header, a padded body and a status footer.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractButton,
    QBoxLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLayoutItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from lab_control.ui.kit import Dot, style_button

PAGE_MARGINS = (20, 16, 20, 16)
PAGE_SPACING = 14


def page_layout(layout: QBoxLayout) -> None:
    layout.setContentsMargins(*PAGE_MARGINS)
    layout.setSpacing(PAGE_SPACING)


def run_button(button: QAbstractButton, running: bool = False) -> None:
    """Start/pause button in the page header (30 px, play / pause icon)."""
    button.setProperty("buttonSize", "compact")
    style_button(button, "", "pause" if running else "play")


def header_row(
    header: QHBoxLayout,
    *,
    connection: QLabel | None = None,
    actions: tuple[QWidget, ...] = (),
) -> None:
    """Hide the text connection state (the device card shows it) and append actions on the right."""
    header.setSpacing(8)
    if connection is not None:
        connection.hide()
    for widget in actions:
        parent = widget.parentWidget()
        if parent is not None and (layout := parent.layout()) is not None:
            _remove(layout, widget)
        header.addWidget(widget)


def empty_label(label: QLabel) -> None:
    """Large dashed placeholder for "no device selected" labels."""
    label.setObjectName("pageEmpty")
    label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    label.setWordWrap(True)


def _remove(layout: QLayout, widget: QWidget) -> bool:
    for index in range(layout.count()):
        item = layout.itemAt(index)
        if item is None:
            continue
        if item.widget() is widget:
            layout.takeAt(index)
            return True
        if (nested := item.layout()) is not None and _remove(nested, widget):
            return True
    return False


def _style_tools(item: QLayoutItem) -> None:
    layout = item.layout()
    if layout is None:
        return
    for index in range(layout.count()):
        child = layout.itemAt(index)
        widget = child.widget() if child else None
        if isinstance(widget, QLabel) and widget.objectName() in {
            "spectrumChartTitle",
            "powerMeterTitle",
            "",
        }:
            widget.setObjectName("cardTitle")
        elif isinstance(widget, QPushButton) and widget.objectName() != "primaryButton":
            widget.setProperty("buttonSize", "compact")


def chart_card(chart: QFrame, *, status: QLabel | None = None, footer_extra: tuple[QWidget, ...] = ()) -> Dot:
    """Turn a chart frame's layout into card head / body / foot.

    The first layout item becomes the head (title and tools), ``status`` and ``footer_extra`` move
    to the foot behind a status dot, everything else stays in the padded body. Returns the dot.
    """
    layout = chart.layout()
    assert isinstance(layout, QVBoxLayout)
    for widget in (status, *footer_extra):
        if widget is not None:
            _remove(layout, widget)
    taken = [layout.takeAt(0) for _ in range(layout.count())]
    items = [item for item in taken if item is not None]
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(0)
    head = QFrame(chart)
    head.setObjectName("cardHead")
    head.setMinimumHeight(48)
    head_layout = QHBoxLayout(head)
    head_layout.setContentsMargins(16, 6, 10, 6)
    head_layout.setSpacing(6)
    if items:
        first = items.pop(0)
        _style_tools(first)
        if (child_layout := first.layout()) is not None:
            head_layout.addLayout(child_layout)
        elif (widget := first.widget()) is not None:
            head_layout.addWidget(widget)
        else:
            head_layout.addItem(first)
    layout.addWidget(head)
    body = QVBoxLayout()
    body.setContentsMargins(12, 10, 14, 8)
    body.setSpacing(8)
    for item in items:
        if (child_layout := item.layout()) is not None:
            body.addLayout(child_layout)
        else:
            body.addItem(item)
    layout.addLayout(body, 1)
    foot = QFrame(chart)
    foot.setObjectName("cardFoot")
    foot_layout = QHBoxLayout(foot)
    foot_layout.setContentsMargins(14, 8, 16, 8)
    foot_layout.setSpacing(6)
    dot = Dot("idle", 7)
    foot_layout.addWidget(dot, 0, Qt.AlignmentFlag.AlignTop)
    if status is not None:
        status.setObjectName("chartStatus")
        foot_layout.addWidget(status, 1)
    else:
        foot_layout.addStretch(1)
    for widget in footer_extra:
        if isinstance(widget, QLabel):
            widget.setObjectName("chartMeta")
        foot_layout.addWidget(widget)
    layout.addWidget(foot)
    chart.setProperty("chartCard", True)
    return dot


def show_state(dot: Dot, task: dict, connected: bool) -> None:
    """Status dot: red error / recording, green running (pulsing), grey idle."""
    dot.set_status(
        "err"
        if task.get("error") or not connected or task.get("recording")
        else "ok"
        if task.get("running")
        else "idle",
        live=bool(task.get("running")),
    )


__all__ = ["chart_card", "empty_label", "header_row", "page_layout", "run_button", "show_state"]
