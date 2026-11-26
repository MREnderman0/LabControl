from PySide6.QtGui import QIcon, QPainter
from PySide6.QtWidgets import QWidget

from lab_control.app import ICON_PATH


class BrandIcon(QWidget):
    """Paint the desktop icon at the current screen's device pixel ratio."""

    def __init__(self):
        super().__init__()
        self.setObjectName("brandLogo")
        self.setAccessibleName("LabControl")
        self.setFixedSize(30, 30)
        self.icon = QIcon(str(ICON_PATH))

    def paintEvent(self, event):  # noqa: N802
        with QPainter(self) as painter:
            self.icon.paint(painter, self.rect())
