import asyncio
from types import SimpleNamespace

import pytest

from lab_control.agent.connector import client_call
from lab_control.application.agents import single_frame
from lab_control.domain.instruments import DeviceError
from lab_control.storage.frame_files import directory


def test_export_directory_rejects_linked_parents(tmp_path, monkeypatch):
    root = tmp_path / "exports"
    link = root / "linked"
    monkeypatch.setattr(type(link), "is_junction", lambda self: self == link)
    assert directory(str(root / "测量"), root) == root / "测量"
    with pytest.raises(ValueError):
        directory(str(link / "sub"), root)
    with pytest.raises(ValueError):
        directory(str(tmp_path / "elsewhere"), root)


def test_archive_limits_include_preexisting_downloads(tmp_path, monkeypatch):
    saved = tmp_path / "previous-task"
    saved.mkdir()
    frame = saved / "frame.csv"
    frame.write_bytes(b"sample")
    monkeypatch.setattr(single_frame, "ARCHIVE_TASKS", 1)
    monkeypatch.setattr(single_frame, "ARCHIVE_BYTES", frame.stat().st_size)
    single_frame.check_archive_capacity(tmp_path, new_task=False)
    with pytest.raises(DeviceError) as count_error:
        single_frame.check_archive_capacity(tmp_path, new_task=True)
    assert count_error.value.code == "storage_full"
    monkeypatch.setattr(single_frame, "ARCHIVE_BYTES", frame.stat().st_size - 1)
    with pytest.raises(DeviceError) as size_error:
        single_frame.check_archive_capacity(tmp_path, new_task=False)
    assert size_error.value.code == "storage_full" and frame.read_bytes() == b"sample"


def test_declined_client_download_leaves_no_directory(tmp_path):
    folder = tmp_path / "pending"

    class Client:
        async def call_tool(self, name, arguments):
            assert name == "get_task_status"
            return SimpleNamespace(is_error=True)

    args = {"task_id": "task", "grant_id": "1" * 32, "directory": str(folder)}
    result = asyncio.run(client_call(Client(), {"export_root": str(tmp_path)}, True, "download_frame", args))
    assert result.is_error and not folder.exists()
    result = asyncio.run(client_call(Client(), {}, True, "download_frame", {**args, "directory": 42}))
    assert result.is_error and not folder.exists()
