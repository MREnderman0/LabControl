"""Desktop software updates: brand version chip, background checks and the update dialog.

Checks and downloads run on one background thread and never touch instrument sessions. Automatic
checks are rare (once shortly after start, then every few hours) and only run in the real-device
workstation; the dialog can always check on demand.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from urllib.error import HTTPError, URLError

from PySide6.QtCore import QObject, QPoint, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QGraphicsDropShadowEffect,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from shiboken6 import isValid

from lab_control.deployment import software_update as updates
from lab_control.ui.kit import IconButton, IconTile, Pill, Separator, VersionChip, icons, label, set_property
from lab_control.ui.styles import tokens as tk

FIRST_CHECK_DELAY_MS = 90_000  # Let startup scans and device sessions settle first.
CHECK_INTERVAL_MS = 6 * 60 * 60 * 1000
RECHECK_ON_OPEN_S = 30 * 60  # Opening the dialog reuses a recent answer instead of asking again.


def busy_reason(runtime, watch_recordings):
    if any(not run.done.is_set() for run in watch_recordings):
        return "监视列表正在记录或保存"
    service = runtime.monitoring
    states = service.snapshots().values()
    if any(s.get("recording") or s.get("recording_preparing") for s in states):
        return "正在记录或准备写盘"
    if any(s.get("capture_saving") or s.get("display_saving") for s in states):
        return "正在导出采集文件"
    if any(s.get("running") or s.get("pending") or s.get("stopping") for s in states):
        return "正在采集或等待采集停止"
    with service.lock:
        if any(not item.finished.done() for item in service.recordings):
            return "记录正在收尾或转换 CSV"
    processing = runtime._processing
    if processing and any(j["state"] in {"queued", "running"} for j in processing.snapshot().values()):
        return "正在处理或导出历史数据"
    if runtime.discovery.is_scanning:
        return "正在扫描设备"
    sessions = runtime.sessions.snapshots()
    if sessions:
        return f"仍有 {len(sessions)} 个设备会话，请先断开"
    return ""


def failure_text(error: BaseException, action: str = "检查更新") -> str:
    """Readable reason; our own validation errors are already Chinese sentences."""
    if isinstance(error, HTTPError):
        return f"{action}失败：更新源返回 HTTP {error.code}，请稍后重试。"
    if isinstance(error, (URLError, TimeoutError, ConnectionError)):
        return f"{action}失败：无法连接腾讯 CNB 更新源，请检查网络后重试。"
    if isinstance(error, ValueError) and str(error):
        return f"{action}失败：{error}"
    return f"{action}失败：{error or type(error).__name__}"


def automatic_checks_enabled(runtime) -> bool:
    """Only the real-device workstation polls; demo windows, tests and review scripts stay offline."""
    return bool(runtime.hardware) and os.environ.get("LABCONTROL_UPDATE_CHECK", "1") != "0"


class SoftwareUpdates(QObject):
    """Update state shared by the sidebar chip and the dialog.

    ``state`` is one of idle, checking, current, available, downloading, ready, failed.
    """

    changed = Signal()
    _finished = Signal(str, object, object)
    _progress = Signal(int)

    def __init__(self, window, *, automatic: bool | None = None) -> None:
        super().__init__(window)
        self.host = window
        self.state = "idle"
        self.release: updates.Release | None = None
        self.latest = ""
        self.package = None
        self.error = ""
        self.notice = ""
        self.percent = 0
        self.checked_at: float | None = None
        self.armed = False
        self.job: threading.Thread | None = None
        self.cancel = threading.Event()
        self._closed = threading.Event()
        self._finished.connect(self._done)
        self._progress.connect(self._on_progress)
        self.automatic = automatic_checks_enabled(window.runtime) if automatic is None else automatic
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setTimerType(Qt.TimerType.VeryCoarseTimer)
        self.timer.timeout.connect(self.check)
        if self.automatic:
            self.timer.start(FIRST_CHECK_DELAY_MS)

    # ------------------------------------------------------------------ presentation
    @property
    def busy(self) -> bool:
        return self.job is not None

    @property
    def has_update(self) -> bool:
        return self.release is not None

    def checked_text(self) -> str:
        if self.checked_at is None:
            return "尚未检查"
        return "上次检查 " + time.strftime("%H:%M", time.localtime(self.checked_at))

    def summary(self) -> str:
        current = f"LabControl {updates.VERSION}"
        if self.state == "downloading":
            return f"{current} · 正在下载 {self.release.version if self.release else ''}  {self.percent}%"
        if self.state == "ready" and self.release:
            return f"{current} · {self.release.version} 已下载，点击安装"
        if self.release:
            return f"{current} · 有新版本 {self.release.version}，点击查看"
        if self.state == "current":
            return f"{current} · 已是最新（{self.checked_text()}）"
        if self.state == "checking":
            return f"{current} · 正在检查更新"
        if self.state == "failed":
            return f"{current} · 检查更新失败，点击重试"
        return f"{current} · 点击检查更新"

    # ------------------------------------------------------------------ background work
    def _submit(self, operation, function, *args) -> None:
        def run():
            try:
                result, error = function(*args), None
            except Exception as exc:
                result, error = None, exc
            if not self._closed.is_set() and isValid(self):
                try:
                    self._finished.emit(operation, result, error)
                except RuntimeError:
                    pass

        # Checks are read-only. A stalled network read must never hold the process open.
        # Downloads finish removing their temporary file on cancellation.
        self.job = threading.Thread(
            target=run, name=f"software-update-{operation}", daemon=operation == "check"
        )
        self.job.start()
        self.changed.emit()

    def _emit_progress(self, percent: int) -> None:
        if not self._closed.is_set() and isValid(self):
            try:
                self._progress.emit(percent)
            except RuntimeError:
                pass

    def check(self) -> None:
        if self._closed.is_set() or self.armed:
            return
        self.timer.stop()
        if self.busy:
            self._schedule()
            return
        self.error = ""
        if self.release is None:
            self.state = "checking"
        self._submit("check", updates.check_release)

    def check_if_stale(self) -> None:
        if self.checked_at is None or time.time() - self.checked_at > RECHECK_ON_OPEN_S:
            self.check()

    def download(self) -> None:
        if self._closed.is_set() or self.release is None or self.busy or self.armed:
            return
        if self.package is not None:
            self.apply()
            return
        self.cancel = threading.Event()
        self.state, self.percent, self.error, self.notice = "downloading", 0, "", ""
        self._submit(
            "download",
            updates.download_release,
            self.release,
            self.host.runtime.repository.root / "maintenance" / "software-updates",
            self._emit_progress,
            self.cancel,
        )

    def cancel_download(self) -> None:
        self.cancel.set()

    def _on_progress(self, percent: int) -> None:
        if not self._closed.is_set() and self.state == "downloading" and percent != self.percent:
            self.percent = percent
            self.changed.emit()

    def _schedule(self) -> None:
        if self.automatic and not self.timer.isActive():
            self.timer.start(CHECK_INTERVAL_MS)

    def _done(self, operation: str, result, error) -> None:
        self.job = None
        if self._closed.is_set():
            return
        if operation == "check":
            self.checked_at = time.time()
            if error is not None:
                self.error = failure_text(error)
                if self.release is None:
                    self.state = "failed"
            elif updates.version_key(result.version) > updates.version_key(updates.VERSION):
                self.latest = result.version
                if self.release != result:
                    self.release, self.package = result, None
                    self.notice = ""
                self.state = "ready" if self.package is not None else "available"
            else:
                self.release = self.package = None
                self.notice = ""
                self.latest = updates.VERSION
                self.state = "current"
            self._schedule()
        elif self.cancel.is_set() or error is not None:
            self.package = None
            self.state = "available"
            self.error = "下载已取消。" if self.cancel.is_set() else failure_text(error, "下载")
        else:
            self.package = result
            self.state = "ready"
            self.apply()
            return
        self.changed.emit()

    # ------------------------------------------------------------------ install
    def apply(self) -> None:
        runtime = self.host.runtime
        self.error = self.notice = ""
        try:
            updates.verify_file(self.package, self.release)
        except (ValueError, OSError) as exc:
            self.package = None
            self.state = "available"
            self.error = f"安装包需重新下载。{exc}"
            self.changed.emit()
            return
        if not runtime.hardware:
            self.notice = "演示版已下载并校验安装包。请在真实设备版 LabControl 中安装。"
            self.changed.emit()
            return
        reason = busy_reason(runtime, self.host.watch_hub.recordings.values())
        if reason:
            self.notice = f"安装包已就绪。{reason}，完成后再点“立即安装”。"
            self.changed.emit()
            return
        try:
            runtime.begin_maintenance()
        except Exception as exc:
            self.error = str(exc)
            self.changed.emit()
            return
        self.armed = True
        self.timer.stop()
        self.changed.emit()
        QTimer.singleShot(0, self.host.close)

    def shutdown(self) -> None:
        """Disarm callbacks before the main window starts releasing its resources."""
        self._closed.set()
        self.timer.stop()
        self.cancel.set()

    def after_shutdown(self) -> None:
        if not self.armed or self.package is None or self.release is None:
            return
        try:
            updates.verify_file(self.package, self.release)
            env = {**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"}
            env.pop("LABCONTROL_RELAUNCH_CONFIG", None)
            if "--config" in sys.argv:
                from pathlib import Path

                env["LABCONTROL_RELAUNCH_CONFIG"] = str(
                    Path(sys.argv[sys.argv.index("--config") + 1]).resolve()
                )
            subprocess.Popen(
                [str(self.package), "/S", "/RELAUNCH", f"/WAITPID={os.getpid()}"],
                cwd=self.package.parent,
                env=env,
            )
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self.host, "无法启动更新", f"{exc}\n可重新开启软件重试。")


class UpdateChip(VersionChip):
    """Version beside the brand name; green with an arrow while a newer release is available."""

    def __init__(self, updates_state: SoftwareUpdates, parent: QWidget | None = None) -> None:
        super().__init__(f"v{updates.VERSION}", parent)
        self.updates = updates_state
        self.setAccessibleName("软件版本与更新")
        updates_state.changed.connect(self.sync)
        self.sync()

    def sync(self) -> None:
        if self.updates.has_update:
            self.set_state(f"v{updates.VERSION}", "ok", "upgrade")
        else:
            self.set_state(f"v{updates.VERSION}")
        self.setToolTip(self.updates.summary())


class _VersionCell(QWidget):
    def __init__(self, caption: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(3)
        self.caption = label(caption, "caption", self)
        self.value = label("", "", self)
        self.value.setObjectName("updateVersionValue")
        column.addWidget(self.caption)
        column.addWidget(self.value)


class UpdateDialog(QDialog):
    """Window-modal sheet over the workstation: versions, release notes and one primary action."""

    WIDTH = 500

    def __init__(self, updates_state: SoftwareUpdates, parent: QWidget) -> None:
        super().__init__(parent)
        self.updates = updates_state
        self.setObjectName("updateOverlay")
        self.setWindowTitle("软件更新")
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        overlay = QGridLayout(self)
        overlay.setContentsMargins(12, 12, 12, 12)
        self.card = QFrame(self)
        self.card.setObjectName("updateCard")
        self.card.setFixedWidth(self.WIDTH)
        overlay.addWidget(self.card, 0, 0, Qt.AlignmentFlag.AlignCenter)
        shadow = QGraphicsDropShadowEffect(self.card)
        shadow.setBlurRadius(36)
        shadow.setOffset(0, 10)
        shadow.setColor(QColor(0, 0, 0, 150))
        self.card.setGraphicsEffect(shadow)
        body = QVBoxLayout(self.card)
        body.setContentsMargins(22, 20, 18, 18)
        body.setSpacing(16)

        header = QHBoxLayout()
        header.setSpacing(12)
        self.tile = IconTile("upgrade", 40, tk.TEXT_2)
        header.addWidget(self.tile, 0, Qt.AlignmentFlag.AlignTop)
        titles = QVBoxLayout()
        titles.setSpacing(2)
        title = label("软件更新", "", self.card)
        title.setObjectName("updateTitle")
        titles.addWidget(title)
        titles.addWidget(label("LabControl · Windows x64 · 腾讯 CNB 发布源", "caption", self.card))
        header.addLayout(titles, 1)
        self.close_button = IconButton("x", "关闭", 30, 16)
        self.close_button.clicked.connect(self.reject)
        header.addWidget(self.close_button, 0, Qt.AlignmentFlag.AlignTop)
        body.addLayout(header)

        versions = QFrame(self.card)
        versions.setObjectName("updateVersions")
        row = QHBoxLayout(versions)
        row.setContentsMargins(16, 12, 14, 12)
        row.setSpacing(14)
        self.current = _VersionCell("当前版本", versions)
        self.current.value.setText(f"v{updates.VERSION}")
        row.addWidget(self.current)
        self.arrow = QLabel(versions)
        self.arrow.setPixmap(icons.icon("arrow", tk.TEXT_3).pixmap(QSize(16, 16)))
        row.addWidget(self.arrow, 0, Qt.AlignmentFlag.AlignBottom)
        self.latest = _VersionCell("最新版本", versions)
        row.addWidget(self.latest)
        row.addStretch(1)
        self.status = Pill("", "neutral", dot=True)
        row.addWidget(self.status, 0, Qt.AlignmentFlag.AlignVCenter)
        body.addWidget(versions)

        self.notes_box = QWidget(self.card)
        notes_column = QVBoxLayout(self.notes_box)
        notes_column.setContentsMargins(0, 0, 0, 0)
        notes_column.setSpacing(8)
        self.notes_title = label("", "heading", self.notes_box)
        notes_column.addWidget(self.notes_title)
        notes_frame = QFrame(self.notes_box)
        notes_frame.setObjectName("updateNotesFrame")
        notes_inner = QVBoxLayout(notes_frame)
        notes_inner.setContentsMargins(12, 10, 6, 10)
        self.notes_scroll = QScrollArea(notes_frame)
        self.notes_scroll.setObjectName("updateNotesScroll")
        self.notes_scroll.setWidgetResizable(True)
        self.notes_scroll.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.notes_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.notes_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.notes = label("", "", self.notes_scroll)
        self.notes.setObjectName("updateNotes")
        self.notes.setWordWrap(True)
        self.notes.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.notes.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.notes_scroll.setWidget(self.notes)
        notes_inner.addWidget(self.notes_scroll)
        notes_column.addWidget(notes_frame)
        body.addWidget(self.notes_box)

        self.progress = QProgressBar(self.card)
        self.progress.setObjectName("updateProgress")
        self.progress.setRange(0, 100)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(6)
        body.addWidget(self.progress)
        self.message = label("", "", self.card)
        self.message.setObjectName("updateMessage")
        self.message.setWordWrap(True)
        body.addWidget(self.message)

        body.addWidget(Separator(parent=self.card))
        footer = QHBoxLayout()
        footer.setSpacing(8)
        self.checked = label("", "caption", self.card)
        footer.addWidget(self.checked, 1)
        self.check_button = QPushButton("检查更新", self.card)
        self.check_button.setIcon(icons.icon("refresh", tk.TEXT, disabled=tk.TEXT_DISABLED))
        self.check_button.setIconSize(QSize(15, 15))
        self.check_button.clicked.connect(self.updates.check)
        self.cancel_button = QPushButton("取消下载", self.card)
        self.cancel_button.clicked.connect(self.updates.cancel_download)
        self.primary = QPushButton("", self.card)
        self.primary.setProperty("variant", "primary")
        self.primary.setIcon(icons.icon("download", tk.ACCENT_INK, disabled=tk.TEXT_DISABLED))
        self.primary.setIconSize(QSize(15, 15))
        self.primary.clicked.connect(self.updates.download)
        for button in (self.check_button, self.cancel_button, self.primary):
            button.setProperty("buttonSize", "dialog")
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            footer.addWidget(button)
        body.addLayout(footer)

        self.updates.changed.connect(self.sync)
        parent.installEventFilter(self)
        self.sync()

    # ------------------------------------------------------------------ state
    def sync(self) -> None:
        u = self.updates
        if u.armed:
            self.accept()
            return
        release = u.release
        state = u.state
        available = release is not None
        self.tile.set_icon("upgrade", tk.OK if available else tk.TEXT_2)
        self.arrow.setVisible(available)
        self.latest.value.setText(f"v{u.latest}" if u.latest and state != "failed" else "—")
        set_property(self.latest.value, "kind", "ok" if available else None)
        if state == "checking":
            self.status.set("正在检查", "info", live=True, icon="", dot=True)
        elif state == "downloading":
            self.status.set(f"下载中 {u.percent}%", "info", live=True, icon="", dot=True)
        elif state == "ready":
            self.status.set("可以安装", "ok", icon="check")
        elif available:
            self.status.set("有可用更新", "ok", icon="upgrade")
        elif state == "current":
            self.status.set("已是最新", "neutral", icon="check")
        elif state == "failed":
            self.status.set("检查失败", "err", icon="alert")
        else:
            self.status.set("尚未检查", "neutral", icon="", dot=True)
        if release is not None:
            self.notes_title.setText(f"v{release.version} 更新说明")
            self.notes.setText(release.notes.strip() or "本次发布没有附带说明。")
        self.notes_box.setVisible(available)
        self.progress.setVisible(state == "downloading")
        self.progress.setValue(u.percent)
        text, kind = self._message()
        self.message.setText(text)
        self.message.setVisible(bool(text))
        set_property(self.message, "kind", kind or None)
        self.checked.setText(
            u.checked_text() + (" · 每 6 小时自动检查" if u.automatic else " · 演示版仅手动检查")
        )
        self.check_button.setVisible(state != "downloading")
        self.check_button.setEnabled(not u.busy)
        checking = u.busy and state != "downloading"
        self.check_button.setText("正在检查…" if checking else "检查更新")
        self.cancel_button.setVisible(state == "downloading")
        self.primary.setVisible(available and state != "downloading")
        self.primary.setEnabled(not u.busy)
        self.primary.setText("立即安装" if u.package is not None else "下载并安装")
        if release is not None:
            self.notes_scroll.setMaximumHeight(
                min(200, max(48, self.notes.heightForWidth(self.card.width() - 72) + 4))
            )

    def _message(self) -> tuple[str, str]:
        u = self.updates
        if u.error:
            return u.error, "err"
        if u.notice:
            return u.notice, "warn"
        if u.state == "downloading":
            return "正在下载安装包。下载在后台进行，可关闭此窗口继续工作。", ""
        if u.state == "available":
            return "安装前请先停止采集、完成写盘并断开设备；安装完成后自动重新启动。", ""
        return "", ""

    # ------------------------------------------------------------------ geometry and input
    def _cover_parent(self) -> None:
        host = self.parentWidget()
        if host is None:
            return
        self.card.setFixedWidth(min(self.WIDTH, max(1, host.width() - 24)))
        self.setGeometry(QRect(host.mapToGlobal(QPoint(0, 0)), host.size()))

    def present(self) -> None:
        self._cover_parent()
        self.open()
        (self.primary if self.primary.isVisible() else self.check_button).setFocus()
        self.updates.check_if_stale()

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        if watched is self.parentWidget() and event.type() in (event.Type.Move, event.Type.Resize):
            self._cover_parent()
        return False

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        scrim = QColor(tk.SCRIM)
        scrim.setAlphaF(0.62)
        painter.fillRect(self.rect(), scrim)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if not self.card.geometry().contains(event.position().toPoint()):
            self.reject()
            return
        super().mousePressEvent(event)

    def done(self, result: int) -> None:
        parent = self.parentWidget()
        if parent is not None:
            parent.removeEventFilter(self)
        super().done(result)


def open_update_dialog(window, updates_state: SoftwareUpdates) -> UpdateDialog:
    existing = getattr(window, "_update_dialog", None)
    if existing is not None and isValid(existing) and existing.isVisible():
        existing.raise_()
        return existing
    dialog = UpdateDialog(updates_state, window)
    window._update_dialog = dialog
    dialog.present()
    return dialog


__all__ = [
    "SoftwareUpdates",
    "UpdateChip",
    "UpdateDialog",
    "busy_reason",
    "open_update_dialog",
]
