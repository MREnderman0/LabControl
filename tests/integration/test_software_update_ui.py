"""Brand version chip, throttled update checks and the update dialog (no network)."""

import hashlib
import io
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.error import URLError

import pytest
from PySide6.QtCore import QPoint, QRect
from PySide6.QtWidgets import QWidget

from lab_control.application.runtime import ApplicationRuntime
from lab_control.deployment import software_update as sw
from lab_control.ui.pages import software_update as page


def _newer_version() -> str:
    major, minor, patch = (int(part) for part in sw.VERSION.split("-")[0].split("."))
    return f"{major}.{minor}.{patch + 1}"


class _Host(QWidget):
    def __init__(self, root, hardware=False):
        super().__init__()
        self.runtime = SimpleNamespace(hardware=hardware, repository=SimpleNamespace(root=root))
        self.watch_hub = SimpleNamespace(recordings={})
        self._shutdown_started = False


def _finish(qtbot, state):
    qtbot.waitUntil(lambda: state.job is None, timeout=5000)


def test_chip_turns_green_only_for_a_newer_release(qtbot, tmp_path, monkeypatch):
    host = _Host(tmp_path)
    qtbot.addWidget(host)
    state = page.SoftwareUpdates(host)
    chip = page.UpdateChip(state, host)
    assert not state.automatic and not state.timer.isActive()
    assert (chip.text(), chip.kind, chip.icon_name) == (f"v{sw.VERSION}", "neutral", "")

    newer = _newer_version()
    monkeypatch.setattr(sw, "check_release", lambda: sw.Release(newer, "说明", 10, "a" * 64))
    state.check()
    _finish(qtbot, state)
    assert state.state == "available" and (chip.kind, chip.icon_name) == ("ok", "upgrade")
    assert newer in chip.toolTip()

    monkeypatch.setattr(sw, "check_release", lambda: sw.Release(sw.VERSION, "", 10, "a" * 64))
    state.check()
    _finish(qtbot, state)
    assert state.state == "current" and state.release is None and chip.kind == "neutral"

    def offline():
        raise URLError("getaddrinfo failed")

    monkeypatch.setattr(sw, "check_release", offline)
    state.check()
    _finish(qtbot, state)
    assert state.state == "failed" and "无法连接" in state.error and chip.kind == "neutral"
    state.shutdown()


def test_automatic_checks_are_rare_and_only_in_the_real_device_workstation(qtbot, tmp_path, monkeypatch):
    monkeypatch.delenv("LABCONTROL_UPDATE_CHECK", raising=False)
    assert page.FIRST_CHECK_DELAY_MS >= 60_000
    assert page.CHECK_INTERVAL_MS >= 6 * 60 * 60 * 1000
    host = _Host(tmp_path, hardware=True)
    qtbot.addWidget(host)
    state = page.SoftwareUpdates(host)
    assert state.automatic and state.timer.isActive()
    assert state.timer.interval() == page.FIRST_CHECK_DELAY_MS
    calls = []

    def release():
        calls.append(1)
        return sw.Release(sw.VERSION, "", 10, "a" * 64)

    monkeypatch.setattr(sw, "check_release", release)
    state.timer.timeout.emit()
    _finish(qtbot, state)
    assert len(calls) == 1
    assert state.timer.isActive() and state.timer.interval() == page.CHECK_INTERVAL_MS
    state.check_if_stale()  # Opening the dialog right after a check reuses the answer.
    assert state.job is None and len(calls) == 1
    monkeypatch.setenv("LABCONTROL_UPDATE_CHECK", "0")
    assert not page.automatic_checks_enabled(host.runtime)
    assert not page.automatic_checks_enabled(SimpleNamespace(hardware=False))
    state.timer.stop()
    state.shutdown()


def test_dialog_downloads_and_demo_mode_only_verifies(qtbot, tmp_path, monkeypatch):
    contents = b"signed setup" * 64
    release = sw.Release(
        _newer_version(), "第一行\n第二行", len(contents), hashlib.sha256(contents).hexdigest()
    )
    monkeypatch.setattr(sw, "check_release", lambda: release)
    monkeypatch.setattr(sw, "open_url", lambda _url: io.BytesIO(contents))
    host = _Host(tmp_path)
    qtbot.addWidget(host)
    host.resize(1000, 720)
    host.show()
    state = page.SoftwareUpdates(host)
    dialog = page.open_update_dialog(host, state)
    _finish(qtbot, state)
    assert dialog.isVisible() and dialog.status.text() == "有可用更新"
    assert dialog.notes.text() == "第一行\n第二行"
    assert dialog.primary.isVisible() and dialog.primary.text() == "下载并安装"
    assert page.open_update_dialog(host, state) is dialog

    dialog.primary.click()
    _finish(qtbot, state)
    assert state.package is not None and state.package.read_bytes() == contents
    assert state.state == "ready" and "演示版" in state.notice and not state.armed
    assert dialog.primary.text() == "立即安装" and dialog.message.text() == state.notice
    dialog.reject()
    qtbot.waitUntil(lambda: not dialog.isVisible())
    state.shutdown()


def test_workstation_shows_version_beside_brand_not_in_settings(qtbot, tmp_path):
    from lab_control.ui.real_window import RealWindow

    runtime = ApplicationRuntime((), root=tmp_path, hardware=False)
    window = RealWindow(runtime)
    qtbot.addWidget(window)
    window.timer.stop()
    try:
        assert window.version_chip.text() == f"v{sw.VERSION}"
        assert not window.updates.automatic and not window.updates.timer.isActive()
        assert not hasattr(window, "update_panel")
        window.updates.release = sw.Release(_newer_version(), "", 1, "a" * 64)
        window.updates.changed.emit()
        assert window.version_chip.kind == "ok" and not window.brand_logo.badge
        window.toggle_sidebar()
        assert window.version_chip.isHidden() and window.brand_logo.badge
        window.toggle_sidebar()
        assert not window.version_chip.isHidden() and not window.brand_logo.badge
    finally:
        window.controller.close()
        window.closing = True
        window.bridge.worker.shutdown()
        window.updates.shutdown()
        runtime.close()


def test_cancel_during_cached_verification_requires_a_new_install_click(qtbot, tmp_path, monkeypatch):
    data = b"cached installer"
    release = sw.Release(_newer_version(), "更新说明", len(data), hashlib.sha256(data).hexdigest())
    cache = tmp_path / "maintenance/software-updates" / release.filename
    cache.parent.mkdir(parents=True)
    cache.write_bytes(data)
    entered, resume = threading.Event(), threading.Event()
    verify = sw.verify_file

    def slow_verify(path, expected):
        if threading.current_thread() is not threading.main_thread():
            entered.set()
            assert resume.wait(5)
        verify(path, expected)

    monkeypatch.setattr(sw, "verify_file", slow_verify)
    monkeypatch.setattr(sw, "check_release", lambda: release)
    monkeypatch.setattr(page, "busy_reason", lambda *_: "")
    host = _Host(tmp_path, hardware=True)
    host.runtime.begin_maintenance = Mock()
    qtbot.addWidget(host)
    state = page.SoftwareUpdates(host, automatic=False)
    dialog = page.open_update_dialog(host, state)
    _finish(qtbot, state)
    try:
        dialog.primary.click()
        qtbot.waitUntil(entered.is_set)
        dialog.cancel_button.click()
    finally:
        resume.set()
    _finish(qtbot, state)
    assert not state.armed and state.package is None and "取消" in dialog.message.text()
    host.runtime.begin_maintenance.assert_not_called()
    dialog.primary.click()
    _finish(qtbot, state)
    assert state.armed
    host.runtime.begin_maintenance.assert_called_once()
    state.shutdown()


@pytest.mark.parametrize("withdrawn", [True, False])
def test_recheck_disables_actions_and_discards_obsolete_package(qtbot, tmp_path, monkeypatch, withdrawn):
    host = _Host(tmp_path)
    qtbot.addWidget(host)
    state = page.SoftwareUpdates(host)
    state.release = sw.Release(_newer_version(), "旧说明", 12, "a" * 64)
    state.latest = state.release.version
    state.state, state.package = "ready", tmp_path / "old-setup.exe"
    state.notice, state.checked_at = "安装包已就绪。请先断开设备。", time.time()
    dialog = page.open_update_dialog(host, state)
    resume = threading.Event()
    replacement = sw.Release(sw.VERSION if withdrawn else _newer_version(), "新说明", 22, "b" * 64)

    def check():
        assert resume.wait(5)
        return replacement

    monkeypatch.setattr(sw, "check_release", check)
    try:
        dialog.check_button.click()
        assert state.busy and not dialog.check_button.isEnabled() and not dialog.primary.isEnabled()
        assert dialog.check_button.text() == "正在检查…"
    finally:
        resume.set()
    _finish(qtbot, state)
    assert state.notice == "" and state.package is None
    assert state.state == ("current" if withdrawn else "available")
    assert dialog.primary.isVisible() == (not withdrawn)
    assert "就绪" not in dialog.message.text() and dialog.check_button.isEnabled()
    state.shutdown()


@pytest.mark.parametrize("size", [(1280, 760), (1024, 608), (853, 460)])
@pytest.mark.parametrize("cached", [True, False])
def test_long_notes_scroll_with_actions_inside_window(qtbot, tmp_path, size, cached, monkeypatch):
    host = _Host(tmp_path)
    qtbot.addWidget(host)
    host.resize(*size)
    host.show()
    release = sw.Release(_newer_version(), "监视记录与设备发现的更新说明。\n" * 80, 20, "c" * 64)
    monkeypatch.setattr(sw, "check_release", lambda: release)
    state = page.SoftwareUpdates(host)
    if cached:
        state.check()
        _finish(qtbot, state)
    dialog = page.open_update_dialog(host, state)
    _finish(qtbot, state)
    qtbot.waitUntil(lambda: dialog.notes_scroll.verticalScrollBar().maximum() > 0)
    for button in (dialog.close_button, dialog.primary, dialog.check_button):
        bounds = QRect(button.mapTo(dialog, QPoint()), button.size())
        assert dialog.rect().contains(bounds) and dialog.rect().size() == host.size()
    state.shutdown()


def test_stalled_automatic_check_does_not_hold_workstation_shutdown(qtbot, tmp_path, monkeypatch):
    from lab_control.ui.real_window import RealWindow

    entered, resume = threading.Event(), threading.Event()

    def stalled_check():
        entered.set()
        assert resume.wait(8)
        return sw.Release(_newer_version(), "迟到的结果", 10, "d" * 64)

    monkeypatch.setattr(sw, "check_release", stalled_check)
    runtime = ApplicationRuntime((), root=tmp_path, hardware=False)
    window = RealWindow(runtime)
    qtbot.addWidget(window)
    window.show()
    state = window.updates
    state.automatic = True
    state.timer.timeout.emit()
    try:
        qtbot.waitUntil(entered.is_set)
        worker = state.job
        assert worker is not None and worker.daemon
        window.close()
        qtbot.waitUntil(lambda: runtime.closed and window.closing, timeout=3000)
        assert not window.isVisible() and worker.is_alive()
    finally:
        resume.set()
    qtbot.waitUntil(lambda: not worker.is_alive())
    assert state.release is None and not state.timer.isActive()
