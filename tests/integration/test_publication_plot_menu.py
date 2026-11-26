import numpy as np
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QMenu

from lab_control.ui.widgets.plot_export import InstrumentMenuCanvas, PublicationPlotWidget
from lab_control.ui.widgets.popup_surface import WorkspaceMenu
from lab_control.ui.widgets.publication_figure import COLORS


def test_right_click_exports_frozen_visible_curves_on_white(qtbot, tmp_path, monkeypatch):
    plot = PublicationPlotWidget(background="black")
    qtbot.addWidget(plot)
    plot.setLabel("bottom", "频率", units="Hz")
    plot.setLabel("left", "偏差")
    curve = plot.plot([1, 10, 100], [0.01, 0.001, 0.0001], name="实测")
    plot.plot([2, 3], [4, 5], name="隐藏").hide()
    plot.setLogMode(x=True, y=True)
    plot.show()
    selected = []
    monkeypatch.setattr(plot, "save_image", selected.append)
    view = plot.getViewBox()

    class Click:
        acceptedItem = view

        def screenPos(self):
            return QPointF(plot.mapToGlobal(plot.rect().center()))

    view.raiseContextMenu(Click())
    menu = next(
        m for m in plot.findChildren(QMenu) if any(a.text() == "保存图像（PNG）" for a in m.actions())
    )
    curve.setData([2, 3], [6, 9])
    menu.actions()[0].trigger()
    menu.close()
    (data,) = selected
    label, x, y = data.curves[0]
    assert len(data.curves) == 1 and label == "实测"
    assert data.log_x and data.log_y and "Hz" in data.xlabel
    np.testing.assert_array_equal(x, [1, 10, 100])
    np.testing.assert_array_equal(y, [0.01, 0.001, 0.0001])
    path = tmp_path / "对数曲线.png"
    data.write(path)
    image = QImage(str(path)).convertToFormat(QImage.Format.Format_RGB32)
    assert image.pixelColor(0, 0).name() == "#ffffff"
    pixels = np.frombuffer(image.constBits(), np.uint32)
    assert np.count_nonzero(pixels == 0xFFFFFFFF) > pixels.size * 0.9
    assert np.any(pixels == (int(COLORS[0][1:], 16) | 0xFF000000))
    assert not plot.scene().contextMenu


def test_preview_plot_offers_only_local_view_and_png_on_axis_or_curve(qtbot):
    plot = InstrumentMenuCanvas()
    qtbot.addWidget(plot)
    plot.resize(480, 280)
    plot.show()
    plot.plot([0, 1, 2], [1, 1, 1])
    plot.autoRange()
    vb = plot.getViewBox()
    positions = [
        QPoint(2, 2),
        plot.mapFromScene(plot.getAxis("bottom").sceneBoundingRect().center()),
        plot.mapFromScene(vb.mapViewToScene(QPointF(1, 1))),
    ]
    for position in positions:
        qtbot.mouseClick(plot.viewport(), Qt.MouseButton.RightButton, pos=position)
        menu = next(m for m in plot.findChildren(WorkspaceMenu) if m.isVisible())
        actions = [a for a in menu.actions() if not a.isSeparator()]
        assert [a.text() for a in actions] == ["恢复视图", "导出 PNG"]
        assert all(a.isEnabled() and a.menu() is None for a in actions)
        qtbot.keyClick(menu, Qt.Key.Key_Escape)
        assert not menu.isVisible()
