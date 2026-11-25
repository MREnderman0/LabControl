"""Shared software-test setup for public and model-owned test trees."""

import gc
import json
import os
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    # Only hand pytest a fresh directory we own: --basetemp is cleared by pytest.
    # Resolve from the checkout, never the current drive or a developer's machine.
    if config.option.basetemp is None and not config.option.collectonly:
        root = Path(__file__).resolve().parent / ".temp"
        root.mkdir(exist_ok=True)
        config.option.basetemp = tempfile.mkdtemp(prefix="p", dir=root)


@pytest.fixture
def frame_export_directory(tmp_path, monkeypatch):
    """The folder selected by the operator in desktop frame-export tests."""
    directory = tmp_path / "手动单帧"
    directory.mkdir()
    monkeypatch.setattr("lab_control.ui.controller.choose_frame_destination", lambda parent: directory)
    return directory


@pytest.fixture
def load_capability_contract():
    """Read versioned model contracts using the original behavior for absent optional fields."""

    def load(path):
        contract = json.loads(path.read_text(encoding="utf-8"))
        for operation in contract["capabilities"]["operations"]:
            operation.setdefault("partial_update", "none")
            for parameter in operation["parameters"].values():
                parameter.setdefault("resolution", None)
        return contract

    return load


@pytest.fixture
def tmp_path(request, tmp_path_factory):
    # Short, unique leaves avoid Windows path limits in nested processing exports.
    path = tmp_path_factory.mktemp("t")
    request.node.add_report_section("setup", "temporary directory", str(path))
    return path


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_teardown(item):
    # pytest-qt schedules deleteLater(); drain it after all fixture finalizers,
    # before the next test replaces styles or creates another model workspace.
    had_widgets = bool(getattr(item, "qt_widgets", ()))
    result = yield
    if had_widgets:
        from PySide6.QtCore import QCoreApplication, QEvent

        if QCoreApplication.instance() is not None:
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            gc.collect()
    return result
