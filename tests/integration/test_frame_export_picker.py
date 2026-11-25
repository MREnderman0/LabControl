from pathlib import Path

from PySide6.QtCore import QSettings, QStandardPaths
from PySide6.QtWidgets import QWidget

from lab_control.ui.widgets import frame_destination


def test_frame_exports_share_persistent_directory_and_cancel_preserves_it(qtbot, tmp_path, monkeypatch):
    parent = QWidget()
    qtbot.addWidget(parent)
    settings = tmp_path / "settings.ini"
    monkeypatch.setattr(
        frame_destination, "QSettings", lambda *_: QSettings(str(settings), QSettings.Format.IniFormat)
    )
    downloads = tmp_path / "重定向的下载文件夹"
    data = tmp_path / "采集数据"
    images = tmp_path / "波形图"
    for folder in (downloads, data, images):
        folder.mkdir()

    def system_folder(location):
        assert location == QStandardPaths.StandardLocation.DownloadLocation
        return str(downloads)

    monkeypatch.setattr(QStandardPaths, "writableLocation", system_folder)
    choices = iter([(downloads, ""), (downloads, str(data)), (images, ""), (images, str(data))])

    def pick_folder(owner, title, initial):
        expected, chosen = next(choices)
        assert owner is parent and Path(initial) == expected
        return chosen

    def pick_image(owner, title, initial, filters, **kwargs):
        assert owner is parent and Path(initial) == data
        return str(images / "频谱"), filters

    monkeypatch.setattr(frame_destination.QFileDialog, "getExistingDirectory", pick_folder)
    monkeypatch.setattr(frame_destination.QFileDialog, "getSaveFileName", pick_image)
    choose = frame_destination.choose_frame_destination
    assert choose(parent) is None
    assert not settings.exists()
    assert choose(parent) == data
    assert choose(parent, png=True) == images / "频谱.png"
    assert choose(parent) is None
    assert choose(parent) == data
    assert QSettings(str(settings), QSettings.Format.IniFormat).value("exports/lastFrameDirectory") == str(
        data
    )
    data.rmdir()
    monkeypatch.setattr(
        frame_destination.QFileDialog, "getExistingDirectory", lambda owner, title, initial: initial
    )
    assert choose(parent) == downloads
