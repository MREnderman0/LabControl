"""Location picker shared by desktop frame data and plot image exports."""

from pathlib import Path

from PySide6.QtCore import QSettings, QStandardPaths
from PySide6.QtWidgets import QFileDialog, QWidget


def frame_export_settings() -> QSettings:
    settings = QSettings("LabControl", "LabControl")
    key = "exports/lastFrameDirectory"
    if not settings.contains(key):
        legacy = QSettings("TF-LabControl", "LabControl")
        if legacy.contains(key):
            settings.setValue(key, legacy.value(key))
    return settings


def choose_frame_destination(parent: QWidget, *, png: bool = False) -> Path | None:
    settings = frame_export_settings()
    key = "exports/lastFrameDirectory"
    last = settings.value(key, "")
    initial = (
        last
        if isinstance(last, str) and last and Path(last).is_dir()
        else QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DownloadLocation)
    )
    if png:
        selected, _ = QFileDialog.getSaveFileName(
            parent, "保存图像", initial, "PNG (*.png)", options=QFileDialog.Option.DontConfirmOverwrite
        )
    else:
        selected = QFileDialog.getExistingDirectory(parent, "选择当前帧的保存位置", initial)
    if not selected:
        return None
    path = Path(selected)
    settings.setValue(key, str(path.parent if png else path))
    return path.with_suffix(".png") if png else path
