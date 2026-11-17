"""Compact readout strip above instrument plots (label over value, separated cells)."""

from __future__ import annotations

from PySide6.QtWidgets import QFrame, QHBoxLayout, QVBoxLayout

from lab_control.ui.widgets.graphite import ElideLabel


class ReadoutCell(QFrame):
    def __init__(self, label: str, *, highlight: bool = False) -> None:
        super().__init__()
        self.setObjectName("readoutCell")
        self.setProperty("highlight", highlight)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 6, 14, 6)
        layout.setSpacing(0)
        self.label = ElideLabel(label)
        self.label.setObjectName("readoutLabel")
        self.value = ElideLabel("—")
        self.value.setObjectName("readoutValue")
        layout.addWidget(self.label)
        layout.addWidget(self.value)

    def set(self, value: str, label: str | None = None) -> None:
        if self.value.text() != value:
            self.value.setText(value)
        if label is not None and self.label.text() != label:
            self.label.setText(label)


class ReadoutStrip(QFrame):
    def __init__(self, cells: dict[str, str], highlight: tuple[str, ...] = ()) -> None:
        super().__init__()
        self.setObjectName("readoutStrip")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.cells = {key: ReadoutCell(label, highlight=key in highlight) for key, label in cells.items()}
        for cell in self.cells.values():
            layout.addWidget(cell)
        layout.addStretch(1)

    def set(self, key: str, value: str, label: str | None = None) -> None:
        self.cells[key].set(value, label)
