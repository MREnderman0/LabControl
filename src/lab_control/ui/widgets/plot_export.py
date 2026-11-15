"""Right-click exports use the same paper renderer as the processing workspace."""

from dataclasses import dataclass
from html import unescape
from pathlib import Path
from typing import Callable

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFileDialog, QMenu, QMessageBox

from .publication_figure import export_figure


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
        menu = QMenu(widget)
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
        menu.popup(event.screenPos().toPoint())


class PublicationPlotWidget(pg.PlotWidget):
    def __init__(self, *args, **kwargs):
        if kwargs.get("plotItem") is None:
            kwargs["viewBox"] = PublicationViewBox()
        super().__init__(*args, **kwargs)
        self.scene().contextMenu = []
        self.snapshot_provider = None

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
        filename, _ = QFileDialog.getSaveFileName(
            self, "保存图像", "", "PNG (*.png)", options=QFileDialog.Option.DontConfirmOverwrite
        )
        if filename:
            try:
                data.write(Path(filename).with_suffix(".png"))
            except (ValueError, OSError) as exc:
                QMessageBox.warning(self, "保存图像", str(exc))
