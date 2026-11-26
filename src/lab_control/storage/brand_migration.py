"""Move the Windows default workspace once, keeping old paths and OS locks valid."""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime
from pathlib import Path

from lab_control.application.leases import FileLease


def _link_directory(link: Path, target: Path) -> None:
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)


def _linked(path: Path) -> bool:
    return path.is_symlink() or path.is_junction()


def _require_idle(path: Path) -> None:
    """SQLite can have idle readers even when no byte-range hardware lock is held."""
    if os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    open_file = kernel.CreateFileW
    open_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    open_file.restype = wintypes.HANDLE
    handle = open_file(str(path), 0x80000000, 0, None, 3, 0, None)
    if handle == wintypes.HANDLE(-1).value:
        raise OSError(f"迁移前无法独占文件 {path}：{ctypes.WinError(ctypes.get_last_error())}")
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle(handle)


def _inventory_state(root: Path, databases: list[Path]) -> dict[Path, str]:
    state = {}
    for relative in databases:
        uri = (root / relative).as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=0)) as db:
            if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise ValueError(f"库存数据库未通过完整性检查：{root / relative}")
            digest = hashlib.sha256()
            for line in db.iterdump():
                digest.update(line.encode("utf-8") + b"\n")
            state[relative] = digest.hexdigest()
    return state


def _copy_verified(source: str, destination: str) -> str:
    shutil.copy2(source, destination)
    with open(source, "rb") as original, open(destination, "rb") as backup:
        if hashlib.file_digest(original, "sha256").digest() != hashlib.file_digest(backup, "sha256").digest():
            raise OSError(f"备份校验失败：{source}")
    return destination


def migrate_default_home(base: Path) -> tuple[Path, Path | None]:
    """Return the canonical home and the verified backup created by this invocation."""
    base = base.resolve(strict=True)
    old, new = base / "TF-LabControl", base / "LabControl"
    if old.exists() and new.exists() and not _linked(new) and old.samefile(new):
        return new, None
    if new.is_dir() and not _linked(new) and not os.path.lexists(old):
        return new, None
    coordination = FileLease(base / ".labcontrol-migration.lock", code="migration_busy")
    try:
        if _linked(new):
            raise ValueError(f"新数据目录已指向另一个位置，请核对：{new}")
        if _linked(old):
            if new.is_dir() and old.exists() and old.samefile(new):
                return new, None
            raise ValueError(f"旧数据目录的链接目标异常，请核对：{old}")
        if not old.exists():
            new.mkdir(exist_ok=True)
            return new, None
        if old.resolve(strict=True) != old or not old.is_dir():
            raise ValueError(f"旧数据目录不在预期位置：{old}")
        if new.exists() and (not new.is_dir() or any(new.iterdir())):
            raise FileExistsError(f"两个数据目录独立存在，请核对后再迁移：{old}、{new}")

        # Lock via a hard link outside the tree so the open handle does not prevent
        # a Windows directory rename. Legacy software still sees exactly the same lock.
        original_gate = old / "hardware.lock"
        if not original_gate.exists():
            original_gate.touch(exist_ok=True)
        gate = base / f".labcontrol-cutover-{uuid.uuid4().hex}.lock"
        gate.hardlink_to(original_gate)
        try:
            lease = FileLease(gate, code="migration_busy")
            try:
                databases = [p.relative_to(old) for p in old.rglob("inventory.sqlite3")]

                def check_idle() -> None:
                    for database in databases:
                        for suffix in ("", "-wal", "-shm"):
                            file = old / (str(database) + suffix)
                            if file.exists():
                                _require_idle(file)

                check_idle()
                state = _inventory_state(old, databases)
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
                backup_root = base / "LabControl-backups"
                if _linked(backup_root) or backup_root.resolve().parent != base:
                    raise ValueError(f"备份目录不在预期位置：{backup_root}")
                backup = backup_root / stamp
                # Refuse to traverse directory junctions into external user storage.
                for folder, children, _ in os.walk(old, followlinks=False):
                    for child in children:
                        if (Path(folder) / child).is_junction():
                            raise ValueError(f"旧目录内存在外部目录链接，请核对：{Path(folder) / child}")
                shutil.copytree(
                    old,
                    backup,
                    symlinks=True,
                    copy_function=_copy_verified,
                    ignore=shutil.ignore_patterns("hardware.lock", "hardware.lock.owner.json"),
                )
                if _inventory_state(backup, databases) != state or _inventory_state(old, databases) != state:
                    raise OSError(f"数据库内容与备份不一致：{backup}")
                check_idle()
                if new.exists():
                    new.rmdir()  # Only an already verified, still-empty destination.
                old.rename(new)
                try:
                    _link_directory(old, new)
                    if not old.samefile(new) or not (old / "hardware.lock").samefile(gate):
                        raise OSError("迁移后的旧目录和互斥锁未指向同一位置")
                    if _inventory_state(new, databases) != state:
                        raise OSError("迁移后的库存与已核验的备份不一致")
                except BaseException:
                    if _linked(old) and old.exists() and old.samefile(new):
                        if old.is_symlink():
                            old.unlink()
                        else:
                            old.rmdir()
                    if not os.path.lexists(old):
                        new.rename(old)
                    raise
                return new, backup
            finally:
                lease.close()
        finally:
            gate.unlink(missing_ok=True)
    finally:
        coordination.close()
