"""Readout strip: label over value cells, used in chart card headers and on its own."""

from __future__ import annotations

from PySide6.QtWidgets import QFrame, QGridLayout, QSizePolicy, QVBoxLayout

from lab_control.ui.kit.base import ElideLabel


class ReadoutCell(QFrame):
    def __init__(self, label: str, *, highlight: bool = False) -> None:
        super().__init__()
        self.setObjectName("readoutCell")
        self.setProperty("highlight", highlight)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 9, 16, 9)
        layout.setSpacing(1)
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
    """``embedded`` drops the outer border so the strip can sit inside a card header."""

    def __init__(
        self, cells: dict[str, str], highlight: tuple[str, ...] = (), *, embedded: bool = False
    ) -> None:
        super().__init__()
        self.setObjectName("readoutStrip")
        self.setProperty("embedded", embedded)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.grid = layout = QGridLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.cells = {key: ReadoutCell(label, highlight=key in highlight) for key, label in cells.items()}
        self.columns = 0
        self._arrange(self.width())

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._arrange(event.size().width())

    def _arrange(self, width: int) -> None:
        columns = min(len(self.cells), max(1, width // 94))
        if columns == self.columns:
            return
        for column in range(self.columns):
            self.grid.setColumnStretch(column, 0)
        self.columns = columns
        compact = columns < len(self.cells)
        for index, cell in enumerate(self.cells.values()):
            layout = cell.layout()
            assert layout is not None
            layout.setContentsMargins(
                7 if compact else 12, 4 if compact else 9, 7 if compact else 12, 4 if compact else 9
            )
            self.grid.addWidget(cell, index // columns, index % columns)
            self.grid.setColumnStretch(index % columns, 1)

    def set(self, key: str, value: str, label: str | None = None) -> None:
        self.cells[key].set(value, label)
