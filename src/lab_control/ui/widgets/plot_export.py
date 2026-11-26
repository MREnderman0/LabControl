"""Right-click exports use the same paper renderer as the processing workspace."""

from dataclasses import dataclass
from html import unescape
from pathlib import Path
from typing import Callable

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QMessageBox

from lab_control.ui.styles import tokens as tk

from .frame_destination import choose_frame_destination
from .popup_surface import WorkspaceMenu
from .publication_figure import export_figure


def apply_axis_theme(widget: pg.PlotWidget) -> None:
    """Graphite axes: 11 px tick labels, muted text and rule colors (visual only)."""
    font = QFont(widget.font())
    font.setPixelSize(11)
    item = widget.getPlotItem()
    for name in ("left", "bottom", "right", "top"):
        axis = item.getAxis(name)
        axis.setStyle(tickFont=font, tickTextOffset=6)
        axis.setPen(pg.mkPen(tk.BORDER_STRONG))
        axis.setTextPen(pg.mkPen(tk.TEXT_3))
        axis.label.setDefaultTextColor(pg.mkColor(tk.TEXT_3))
        # Kept by later setLabel() calls that pass no style of their own.
        axis.labelStyle = {"color": tk.TEXT_3, "font-size": "11px"}


@dataclass
class PlotExport:
    curves: list
    xlabel: str
    ylabel: str
    log_x: bool = False
    log_y: bool = False
    save_raw: Callable | None = None

    def write(self, path):
        export_figure(Path(path), self.curves, self.xlabel, self.ylabel, log_x=self.log_x, log_y=self.log_y)


class PublicationViewBox(pg.ViewBox):
    def raiseContextMenu(self, event):  # noqa: N802
        widget = self.getViewWidget()
        menu = WorkspaceMenu(widget)
        menu.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        data = widget.export_snapshot()
        image = menu.addAction("保存图像（PNG）")
        image.setEnabled(bool(data.curves))
        image.triggered.connect(lambda: widget.save_image(data))
        if data.save_raw is not None:
            raw = menu.addAction("保存原始数据", data.save_raw)
            raw.setEnabled(bool(data.curves))
        if any(self.mouseEnabled()):
            controls = super().getMenu(event)
            if controls is not None:
                menu.addSeparator()
                menu.addMenu(controls)
            self.scene().addParentContextMenus(self, menu, event)
        menu.popup_at(event.screenPos().toPoint())


class PublicationPlotWidget(pg.PlotWidget):
    def __init__(self, *args, **kwargs):
        if kwargs.get("plotItem") is None:
            kwargs["viewBox"] = PublicationViewBox()
        super().__init__(*args, **kwargs)
        self.scene().contextMenu = []
        self.snapshot_provider = None
        apply_axis_theme(self)

    def export_snapshot(self):
        if self.snapshot_provider is not None:
            return self.snapshot_provider()

        def label(axis):
            return f"{axis.labelText} ({axis.labelUnits})" if axis.labelUnits else axis.labelText

        xaxis, yaxis = self.getAxis("bottom"), self.getAxis("left")
        curves = []
        for item in self.listDataItems():
            if not item.isVisible() or not isinstance(item, pg.PlotDataItem):
                continue
            x, y = item.getOriginalDataset()
            if x is not None and y is not None and len(x) and np.any(np.isfinite(x) & np.isfinite(y)):
                curves.append((unescape(item.name() or yaxis.labelText or "曲线"), x.copy(), y.copy()))
        return PlotExport(curves, label(xaxis), label(yaxis), xaxis.logMode, yaxis.logMode)

    def save_image(self, data):
        filename = choose_frame_destination(self, png=True)
        if filename is not None:
            try:
                data.write(filename)
            except (ValueError, OSError) as exc:
                QMessageBox.warning(self, "保存图像", str(exc))


class InstrumentMenuCanvas(PublicationPlotWidget):
    """Use the same instrument menu on the trace, axes and empty canvas."""

    def __init__(self, *args, restore_view=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.restore_view = restore_view or self.autoRange
        self.frame_save_provider: Callable[[], Callable[[], None] | None] | None = None
        self.menu_factory: Callable[[QPoint], WorkspaceMenu] = self.build_menu

    def build_menu(self, position=None):
        snapshot = self.export_snapshot()
        save = self.frame_save_provider() if self.frame_save_provider is not None else None
        return self.view_menu(
            snapshot,
            can_restore=bool(snapshot.curves),
            supports_save=self.frame_save_provider is not None,
            save_frame=save,
        )

    def view_menu(self, snapshot, *, can_restore, supports_save, save_frame):
        menu = WorkspaceMenu(self)
        menu.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        menu.addAction("恢复视图", self.restore_view).setEnabled(can_restore)
        menu.addSeparator()
        if supports_save:
            action = menu.addAction("保存当前帧数据")
            action.setEnabled(save_frame is not None)
            if save_frame is not None:
                action.triggered.connect(save_frame)
        menu.addAction("导出 PNG", lambda: self.save_image(snapshot)).setEnabled(bool(snapshot.curves))
        return menu

    def open_menu(self, position):
        self.menu_factory(position).popup_at(self.viewport().mapToGlobal(position))

    def mousePressEvent(self, event):  # noqa: N802
        if event.button() == Qt.MouseButton.RightButton:
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):  # noqa: N802
        if event.button() == Qt.MouseButton.RightButton:
            self.open_menu(event.position().toPoint())
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    def contextMenuEvent(self, event):  # noqa: N802
        if event.reason() == event.Reason.Keyboard:
            self.open_menu(self.viewport().rect().center())
        event.accept()
