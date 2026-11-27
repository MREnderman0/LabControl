import csv
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QFileDialog

from lab_control.application.watchlists import Preset, WatchRow
from lab_control.storage.watch_trends import append_point, create_batch
from lab_control.ui.pages.watch_trend_view import WatchTrendDialog


def test_trend_menu_exports_current_image_and_complete_row_at_click(qtbot, tmp_path, monkeypatch):
    row = WatchRow("id", "", "scope", 1, "waveform", "平均值", id="mean", unit="V")
    preset = Preset("示例", (row,))
    path = create_batch(tmp_path, preset)
    start = datetime(2026, 11, 27, tzinfo=timezone.utc)

    def insert(db, i):
        # Many more original points than display pixels.
        stamp = (start + timedelta(seconds=i)).isoformat()
        append_point(db, row, "id", "示例", "session", stamp, i, float(i % 3), "V", "正常")

    with closing(sqlite3.connect(path)) as db, db:
        for i in range(2500):
            insert(db, i)
    jobs = []
    ctl = SimpleNamespace(
        runtime=SimpleNamespace(repository=SimpleNamespace(data_root=tmp_path)), watch=jobs.append
    )
    run = SimpleNamespace(
        accepting=True, path=path, root=tmp_path, revision=1, done=SimpleNamespace(is_set=lambda: False)
    )
    hub = SimpleNamespace(ctl=ctl, recordings={preset.id: run}, presets={preset.id: preset})
    dialog = WatchTrendDialog(hub, preset.id, row, None)
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitUntil(lambda: bool(dialog.points[0]))
    vb = dialog.plot.getViewBox()
    vb.setRange(xRange=(start.timestamp() + 10, start.timestamp() + 14), yRange=(0, 2), padding=0)
    dialog.moved()
    qtbot.waitUntil(lambda: len(dialog.points[0]) == 5)
    assert not dialog.follow.isChecked()
    qtbot.mouseClick(dialog.plot.viewport(), Qt.MouseButton.RightButton, pos=dialog.plot.rect().center())
    qtbot.waitUntil(dialog.plot_menu.isVisible)
    actions = dialog.plot_menu.actions()
    assert [a.text() for a in actions] == ["保存图像", "保存数据", "恢复视图"]
    dialog.plot_menu.close()
    files = tmp_path / "输出"
    files.mkdir()
    image_file = files / "窗口.png"
    csv_file = files / "数据.csv"
    expected = dialog.plot.grab().toImage()

    def pick(*args, **kwargs):
        if args[1] == "保存图像":
            return str(image_file), ""
        with closing(sqlite3.connect(path)) as db, db:
            insert(db, 2500)
        return str(csv_file), ""

    monkeypatch.setattr(QFileDialog, "getSaveFileName", pick)
    actions[0].trigger()
    actual = QImage(str(image_file)).convertToFormat(expected.format())
    assert actual == expected
    actions[1].trigger()
    qtbot.waitUntil(lambda: bool(jobs) and jobs[0].done())
    jobs[0].result()
    with csv_file.open(encoding="utf-8-sig", newline="") as fp:
        entries = list(csv.DictReader(fp))
    assert len(entries) == 2500
    assert entries[0]["采集时间"] == start.isoformat() and entries[-1]["数值"] == "0.0"
    actions[2].trigger()
    qtbot.waitUntil(lambda: vb.viewRange()[0][1] >= start.timestamp() + 2500)
    assert dialog.follow.isChecked() and dialog.batches.currentData() == path
    dialog.close()
