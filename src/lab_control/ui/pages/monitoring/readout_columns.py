"""Keep live numeric columns aligned independently of their current text."""

from PySide6.QtWidgets import QHBoxLayout, QSizePolicy, QWidget


def add_readout_column(layout: QHBoxLayout, widget: QWidget, minimum: int, stretch: int = 0) -> None:
    if stretch:
        widget.setMinimumWidth(minimum)
        widget.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
    else:
        widget.setFixedWidth(minimum)
    layout.addWidget(widget, stretch)
