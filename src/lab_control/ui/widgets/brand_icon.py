from PySide6.QtCore import QPointF, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPen
from PySide6.QtWidgets import QWidget

from lab_control.app import ICON_PATH
from lab_control.ui.styles import tokens as tk


class BrandIcon(QWidget):
    """Paint the desktop icon at the current screen's device pixel ratio.

    Clicking opens version and update details; ``set_badge`` adds the green update dot used while
    the sidebar is collapsed and the version chip is hidden.
    """

    clicked = Signal()

    def __init__(self):
        super().__init__()
        self.setObjectName("brandLogo")
        self.setAccessibleName("LabControl")
        self.setFixedSize(30, 30)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.icon = QIcon(str(ICON_PATH))
        self.badge = False

    def set_badge(self, badge: bool) -> None:
        if badge != self.badge:
            self.badge = badge
            self.update()

    def mouseReleaseEvent(self, event):  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit()
        super().mouseReleaseEvent(event)

    def paintEvent(self, event):  # noqa: N802
        with QPainter(self) as painter:
            self.icon.paint(painter, self.rect())
            if self.badge:
                painter.setRenderHint(QPainter.RenderHint.Antialiasing)
                painter.setPen(QPen(QColor(tk.SIDE), 2))
                painter.setBrush(QColor(tk.OK))
                painter.drawEllipse(QPointF(self.width() - 4, 4), 4, 4)
