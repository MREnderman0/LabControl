"""Live and historical watch readings from the same on-disk batch."""

import sqlite3
from bisect import bisect_left
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime
from math import isfinite
from pathlib import Path

import pyqtgraph as pg
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QCheckBox, QDialog, QFileDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from lab_control.storage.paths import filename_part
from lab_control.storage.watch_trends import (
    chart_data,
    export_row_snapshot,
    history,
    last_point_id,
    reading_db,
)
from lab_control.ui.widgets.popup_surface import WorkspaceMenu
from lab_control.ui.widgets.workspace_combo import WorkspaceComboBox


class WatchTrendDialog(QDialog):
    def __init__(self, hub, key, row, parent):
        super().__init__(parent, Qt.WindowType.Window)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.hub, self.key, self.row = hub, key, row
        self.setWindowTitle(f"{hub.presets.get(key).name} · {row.label} · 通道 {row.channel}")
        self.resize(880, 480)
        self.jobs = ThreadPoolExecutor(max_workers=1, thread_name_prefix="watch-history")
        self.pending = None
        self.loaded = None
        self.batch_state = None
        self.points: tuple[list[float], list[float], str] = ([], [], "")
        self.limits = None
        layout = QVBoxLayout(self)
        bar = QHBoxLayout()
        bar.addWidget(QLabel("记录批次"))
        self.batches = WorkspaceComboBox()
        bar.addWidget(self.batches, 1)
        self.follow = QCheckBox("跟随最新")
        self.follow.setChecked(True)
        bar.addWidget(self.follow)
        self.reset = QPushButton("完整曲线")
        bar.addWidget(self.reset)
        layout.addLayout(bar)
        self.plot = pg.PlotWidget(
            enableMenu=False, axisItems={"bottom": pg.DateAxisItem(orientation="bottom")}
        )
        self.plot.scene().contextMenu = []
        self.plot_menu = WorkspaceMenu(self.plot)
        self.image_action = self.plot_menu.addAction("保存图像", self.save_image)
        self.data_action = self.plot_menu.addAction("保存数据", self.save_data)
        self.plot_menu.addAction("恢复视图", self.restore_view)
        self.plot.scene().sigMouseClicked.connect(self.show_plot_menu)
        self.plot.setLabel("bottom", "采集时间")
        self.plot.showGrid(x=True, y=True, alpha=0.18)
        self.curve = self.plot.plot(pen=pg.mkPen("#429fc5", width=2), connect="finite", name=row.label)
        layout.addWidget(self.plot, 1)
        self.note = QLabel()
        self.note.setWordWrap(True)
        layout.addWidget(self.note)
        self.cursor_label = QLabel("移动指针查看采集时间和数值")
        layout.addWidget(self.cursor_label)
        self.plot.scene().sigMouseMoved.connect(self.hover)
        self.plot.getViewBox().sigRangeChangedManually.connect(self.moved)
        self.batches.currentIndexChanged.connect(self.selected)
        self.follow.toggled.connect(self.follow_changed)
        self.reset.clicked.connect(self.restore_view)
        self.timer = QTimer(self, interval=300)
        self.timer.timeout.connect(self.refresh)
        self.finished.connect(self.stop_reader)
        self.timer.start()
        self.refresh()

    def selected(self):
        self.points = ([], [], "")
        self.curve.clear()
        self.cursor_label.setText("移动指针查看采集时间和数值")
        self.full()

    def full(self):
        self.limits = None
        self.loaded = None
        self.plot.enableAutoRange()

    def restore_view(self):
        run = self.hub.recordings.get(self.key)
        if run and run.accepting and run.path == self.batches.currentData():
            self.follow.setChecked(True)
        self.full()

    def show_plot_menu(self, event):
        if event.button() == Qt.MouseButton.RightButton:
            self.image_action.setEnabled(bool(self.points[0]))
            self.data_action.setEnabled(bool(self.batches.currentData()))
            self.plot_menu.popup(event.screenPos().toPoint())

    def save_image(self):
        image = self.plot.grab().toImage()
        filename, _ = QFileDialog.getSaveFileName(
            self, "保存图像", filename_part(self.row.label) + ".png", "PNG (*.png)"
        )
        if filename and not image.save(str(Path(filename).with_suffix(".png")), "PNG"):
            self.hub.ctl.message.emit("趋势图像保存失败")

    def save_data(self):
        path = self.batches.currentData()
        if not path:
            return
        try:
            with closing(reading_db(path)) as db:
                through = last_point_id(db)
        except sqlite3.Error as exc:
            self.hub.ctl.message.emit(f"读取记录失败 · {exc}")
            return
        suggested = filename_part(self.row.label) + f"_CH{self.row.channel}_导出.csv"
        filename, _ = QFileDialog.getSaveFileName(self, "保存数据", suggested, "CSV (*.csv)")
        if filename:
            job = self.jobs.submit(
                export_row_snapshot, path, self.row.id, Path(filename).with_suffix(".csv"), through
            )
            self.hub.ctl.watch(job)

    def follow_changed(self, enabled):
        if enabled:
            self.full()
        else:
            self.moved()

    def moved(self, *_):
        self.follow.blockSignals(True)
        self.follow.setChecked(False)
        self.follow.blockSignals(False)
        self.limits = tuple(self.plot.getViewBox().viewRange()[0])
        self.plot.disableAutoRange()

    def refresh(self):
        run = self.hub.recordings.get(self.key)
        root = self.hub.ctl.runtime.repository.data_root
        state = (root, run.path if run else None, run.done.is_set() if run else None)
        if state != self.batch_state:
            previous = self.batches.currentData()
            latest_selected = self.batches.currentIndex() == 0
            new_batch = self.batch_state is not None and self.batch_state[1] != state[1]
            try:
                matches = history(root, self.key, self.row.id)
                if run and root != run.root:
                    matches += history(run.root, self.key, self.row.id)
            except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
                self.note.setText(f"读取记录目录失败 · {exc}")
                return
            self.batch_state = state
            self.batches.blockSignals(True)
            self.batches.clear()
            for path, stamp, status, error in matches:
                live = run and path == run.path and not run.done.is_set()
                label = {
                    "open": "记录中" if live else "上次记录中断",
                    "error": "写入失败",
                    "complete": "已完成",
                }[status]
                self.batches.addItem(
                    f"{datetime.fromisoformat(stamp).astimezone():%Y-%m-%d %H:%M:%S} · {label}", path
                )
                self.batches.setItemData(
                    self.batches.count() - 1, error or str(path.parent), Qt.ItemDataRole.ToolTipRole
                )
            if previous and not (new_batch and latest_selected and self.follow.isChecked()):
                idx = self.batches.findData(previous)
                if idx >= 0:
                    self.batches.setCurrentIndex(idx)
            self.batches.blockSignals(False)
            if previous != self.batches.currentData():
                self.selected()
        path = self.batches.currentData()
        if not path:
            self.note.setText("此监视量暂无记录。开始列表记录后，曲线会在这里更新。")
            return
        revision = run.revision if run and run.path == path else 0
        token = path, self.limits, revision
        if self.pending:
            old, job = self.pending
            if not job.done():
                return
            self.pending = None
            if old[:2] == token[:2]:
                try:
                    x, y, unit, gap = job.result()
                    self.points = x, y, unit
                    self.curve.setData(x, y, symbol="o" if len(x) < 60 else None, symbolSize=4)
                    self.plot.setLabel("left", self.row.label, units=unit or self.row.unit)
                    self.note.setText(gap or ("" if x else "等待有效样本"))
                except Exception as exc:
                    self.note.setText(f"曲线读取失败 · {exc}")
                self.loaded = old
        if self.loaded != token:
            width = max(100, self.plot.width())
            self.pending = token, self.jobs.submit(chart_data, path, self.row.id, self.limits, width)

    def hover(self, pos):
        x, y, unit = self.points
        if not x or not self.plot.sceneBoundingRect().contains(pos):
            return
        target = self.plot.getViewBox().mapSceneToView(pos).x()
        i = bisect_left(x, target)
        i = min((j for j in (i - 1, i) if 0 <= j < len(x)), key=lambda j: abs(x[j] - target))
        value = f"{y[i]:.9g} {unit}" if isfinite(y[i]) else "记录间断"
        self.cursor_label.setText(
            f"{datetime.fromtimestamp(x[i]).isoformat(sep=' ', timespec='milliseconds')}   {value}"
        )

    def stop_reader(self, *_):
        self.timer.stop()
        if self.pending:
            self.pending[1].cancel()
        self.jobs.shutdown(wait=False)
