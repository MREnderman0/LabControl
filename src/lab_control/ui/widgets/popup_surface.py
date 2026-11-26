"""Shared popup chrome for choices and action menus, in logical pixels."""

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QMenu, QToolButton

from lab_control.ui.styles import tokens as tk

ROW_HEIGHT = 34
SURFACE_PADDING = 6
SHADOW_MARGIN = 8
POPUP_MARGIN = SURFACE_PADDING + SHADOW_MARGIN


def paint_popup_surface(widget) -> None:
    painter = QPainter(widget)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    panel = widget.rect().adjusted(SHADOW_MARGIN, SHADOW_MARGIN, -SHADOW_MARGIN, -SHADOW_MARGIN)
    painter.setPen(Qt.PenStyle.NoPen)
    for spread in range(SHADOW_MARGIN, 0, -1):
        painter.setBrush(QColor(0, 0, 0, 5 + (SHADOW_MARGIN - spread) * 2))
        painter.drawRoundedRect(panel.adjusted(-spread, -spread, spread, spread), 8 + spread, 8 + spread)
    painter.setBrush(QColor(tk.POPUP))
    painter.setPen(QPen(QColor(tk.BORDER_STRONG), 1))
    painter.drawRoundedRect(panel, 8, 8)


class WorkspaceMenu(QMenu):
    """QAction semantics with the same surface and cancellation policy as choices."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("workspaceActionMenu")
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint)
        self.setWindowFlag(Qt.WindowType.NoDropShadowWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_NoMouseReplay)

    def popup_at(self, point: QPoint) -> None:
        self.popup(point - QPoint(SHADOW_MARGIN, SHADOW_MARGIN))

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        button = self.parentWidget()
        if not isinstance(button, QToolButton) or button.menu() is not self:
            return
        bounds = button.screen().availableGeometry()
        origin = button.mapToGlobal(QPoint(0, 0))
        x = origin.x() - SHADOW_MARGIN
        y = origin.y() + button.height() + 2 - SHADOW_MARGIN
        if y + self.height() > bounds.bottom() + 1:
            y = origin.y() - self.height() - 2 + SHADOW_MARGIN
        self.move(
            max(bounds.left(), min(x, bounds.right() + 1 - self.width())),
            max(bounds.top(), min(y, bounds.bottom() + 1 - self.height())),
        )

    def paintEvent(self, event) -> None:  # noqa: N802
        paint_popup_surface(self)
        super().paintEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self.setAttribute(Qt.WidgetAttribute.WA_NoMouseReplay)
        super().mousePressEvent(event)
