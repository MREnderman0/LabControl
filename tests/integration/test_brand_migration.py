"""An upgrade keeps live WAL data, old file references and OS lock exclusion."""

import os
import shutil
import sqlite3
from contextlib import closing

import pytest

from lab_control.application.leases import FileLease, ProcessLease
from lab_control.domain.instruments import DeviceError
from lab_control.storage import brand_migration as migration


@pytest.mark.skipif(os.name != "nt", reason="Windows installations")
def test_startup_keeps_legacy_inventory_data_and_hardware_lock(tmp_path, monkeypatch):
    from lab_control.storage.runtime import default_home, hardware_lock_home, runtime_home

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.delenv("LAB_CONTROL_HOME", raising=False)
    legacy = legacy_inventory(tmp_path)
    original = (legacy / "inventory.sqlite3").read_bytes()
    assert default_home() == runtime_home() == hardware_lock_home() == legacy
    assert (legacy / "inventory.sqlite3").read_bytes() == original
    assert not (tmp_path / "LabControl").exists()
    assert not (tmp_path / "LabControl-backups").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows installations")
def test_startup_selects_existing_paths_and_keeps_override_separate_from_lock(tmp_path, monkeypatch):
    from lab_control.storage.runtime import default_home, hardware_lock_home, runtime_home

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.delenv("LAB_CONTROL_HOME", raising=False)
    new = tmp_path / "LabControl"
    legacy = tmp_path / "TF-LabControl"
    assert default_home() == new and new.is_dir() and not legacy.exists()
    legacy.mkdir()
    with pytest.raises(FileExistsError):
        default_home()
    assert legacy.is_dir() and new.is_dir()
    external = tmp_path / "external"
    monkeypatch.setenv("LAB_CONTROL_HOME", str(external))
    assert runtime_home() == external
    legacy.rmdir()
    migration._link_directory(legacy, new)
    assert default_home() == hardware_lock_home() == new
    assert runtime_home() == external and legacy.is_junction()


def test_new_workspace_never_creates_legacy_directory(tmp_path):
    new = tmp_path / "LabControl"
    for _ in range(2):
        assert migration.migrate_default_home(tmp_path) == (new, None)
        assert new.is_dir()
        assert not os.path.lexists(tmp_path / "TF-LabControl")


def legacy_inventory(base):
    old = base / "TF-LabControl"
    old.mkdir()
    # Model a cleanly stopped process whose last committed rows are only in the WAL.
    fixture = base / "recording.sqlite3"
    with closing(sqlite3.connect(fixture)) as db:
        db.execute("pragma journal_mode=wal")
        db.execute("create table workspace (key text, value text)")
        db.execute("insert into workspace values (?,?)", ("槽位", "频率计"))
        db.commit()
        for suffix in ("", "-wal", "-shm"):
            shutil.copyfile(str(fixture) + suffix, old / ("inventory.sqlite3" + suffix))
    return old


def test_upgrade_and_repeated_launch_preserve_inventory_and_locks(tmp_path):
    old = legacy_inventory(tmp_path)
    evidence = old / "历史记录.txt"
    evidence.write_text("已配对", encoding="utf-8")
    new, backup = migration.migrate_default_home(tmp_path)
    assert backup is not None and (backup / evidence.name).read_bytes() == evidence.read_bytes()
    assert old.samefile(new)
    for root in (new, backup):
        with closing(sqlite3.connect(root / "inventory.sqlite3")) as db:
            assert db.execute("select * from workspace").fetchall() == [("槽位", "频率计")]
    assert migration.migrate_default_home(tmp_path) == (new, None)
    lease = ProcessLease(old)
    try:
        with pytest.raises(DeviceError):
            ProcessLease(new)
    finally:
        lease.close()


def test_busy_or_conflicting_inventory_is_preserved(tmp_path):
    old = legacy_inventory(tmp_path)
    lock = FileLease(old / "hardware.lock")
    try:
        with pytest.raises(DeviceError):
            migration.migrate_default_home(tmp_path)
        assert not (tmp_path / "LabControl").exists()
    finally:
        lock.close()
    new = tmp_path / "LabControl"
    new.mkdir()
    (new / "inventory.sqlite3").write_bytes(b"independent")
    with pytest.raises(FileExistsError):
        migration.migrate_default_home(tmp_path)
    assert (new / "inventory.sqlite3").read_bytes() == b"independent"


def test_failed_link_rolls_back_without_data_loss(tmp_path, monkeypatch):
    old = legacy_inventory(tmp_path)

    def fail(*args):
        raise OSError("junction creation failed")

    monkeypatch.setattr(migration, "_link_directory", fail)
    with pytest.raises(OSError, match="junction creation failed"):
        migration.migrate_default_home(tmp_path)
    assert not migration._linked(old) and not (tmp_path / "LabControl").exists()
    with closing(sqlite3.connect(old / "inventory.sqlite3")) as db:
        assert db.execute("select count(*) from workspace").fetchone() == (1,)


@pytest.mark.skipif(os.name != "nt", reason="Windows database sharing")
def test_idle_database_connection_blocks_upgrade(tmp_path):
    old = legacy_inventory(tmp_path)
    with closing(sqlite3.connect(old / "inventory.sqlite3")) as db:
        db.execute("select * from workspace").fetchall()
        with pytest.raises(OSError, match="无法独占"):
            migration.migrate_default_home(tmp_path)
    assert old.is_dir() and not (tmp_path / "LabControl").exists()
