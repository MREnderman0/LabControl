"""Current-user installer transaction run from the freshly extracted Windows bundle."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import traceback
import winreg
from contextlib import contextmanager
from pathlib import Path

from lab_control.deployment.software_update import VERSION, version_key

REGKEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\LabControl.Graphite"
INDEX = ".labcontrol-owned.json"
LEGACY = "0.3.0-graphite.1"


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def child(root, name):
    rel = Path(name)
    if (
        not rel.parts
        or rel.is_absolute()
        or rel.drive
        or any(
            p in {".", ".."} or ":" in p or p.endswith((".", " ")) or Path(p).is_reserved() for p in rel.parts
        )
    ):
        raise ValueError(f"安装清单路径无效：{name}")
    path = root / rel
    if path.resolve() != path.absolute() or any(
        p.is_junction() or p.is_symlink() for p in (path, *path.parents) if p != root.parent
    ):
        raise ValueError(f"安装路径包含文件链接：{name}")
    return path


def registration():
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, REGKEY, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY)
    except FileNotFoundError:
        return {}
    with key:
        return {
            name: [value, kind]
            for name, value, kind in (winreg.EnumValue(key, i) for i in range(winreg.QueryInfoKey(key)[1]))
        }


def write_registration(values):
    if not values:
        try:
            winreg.DeleteKeyEx(winreg.HKEY_CURRENT_USER, REGKEY, winreg.KEY_WOW64_64KEY)
        except FileNotFoundError:
            pass
        return
    with winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER, REGKEY, 0, winreg.KEY_ALL_ACCESS | winreg.KEY_WOW64_64KEY
    ) as key:
        for name, (value, kind) in values.items():
            winreg.SetValueEx(key, name, 0, kind, value)
        winreg.FlushKey(key)


def make_shortcut(path, target):
    shell = Path(os.environ["WINDIR"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    script = (
        "$ErrorActionPreference='Stop';"
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:LC_LINK);"
        "$s.TargetPath=$env:LC_TARGET;$s.WorkingDirectory=[IO.Path]::GetDirectoryName($env:LC_TARGET);"
        "$s.Save()"
    )
    subprocess.run(
        [str(shell), "-NoProfile", "-NonInteractive", "-Command", script],
        check=True,
        env={**os.environ, "LC_LINK": str(path), "LC_TARGET": str(target)},
        creationflags=subprocess.CREATE_NO_WINDOW,
    )


@contextmanager
def installation_lock():
    from ctypes import wintypes

    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
    api.CreateMutexW.restype = wintypes.HANDLE
    api.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = api.CreateMutexW(None, False, r"Local\LabControl.Graphite.Install")
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if ctypes.get_last_error() == 183:
            raise RuntimeError("另一个安装程序正在运行")
        yield
    finally:
        api.CloseHandle(handle)


def rollback(dest, txn, plan, link):
    for name in reversed(plan["files"]):
        target = child(dest, name)
        backup = child(txn / "old", name)
        if backup.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(backup, target)
        elif name in plan["fresh"]:
            target.unlink(missing_ok=True)
    if (txn / "old.lnk").is_file():
        shutil.copy2(txn / "old.lnk", link)
    elif not plan["had_link"]:
        link.unlink(missing_ok=True)
    write_registration(plan["registry"])


def discard_transaction(txn, dest):
    if txn.parent != dest.parent or txn.name != "." + dest.name + ".LabControl-upgrade":
        raise ValueError("无效的安装事务目录")
    if txn.is_symlink() or txn.is_junction() or txn.resolve() != txn:
        raise ValueError("安装事务目录包含链接")
    shutil.rmtree(txn)


def install(stage, dest, programs, package):
    if not dest.is_absolute() or dest == Path(dest.anchor) or dest.resolve() != dest:
        raise ValueError("请选择独立的程序安装目录")
    marker = "LabControl.Graphite"
    txn = dest.with_name("." + dest.name + ".LabControl-upgrade")
    link = programs / "LabControl 石墨版.lnk"
    journal = txn / "transaction.json"
    if txn.exists():
        prior = json.loads(child(txn, "transaction.json").read_text("utf-8"))
        if prior["product"] != marker or Path(prior["dest"]) != dest or Path(prior["link"]) != link:
            raise ValueError("安装事务与目标目录不匹配")
        if not (txn / "committed").exists():
            rollback(dest, txn, prior, link)
        discard_transaction(txn, dest)
    oldreg = registration()
    registered = oldreg.get("InstallLocation", [""])[0]
    oldversion = oldreg.get("DisplayVersion", [""])[0]
    if registered and (Path(registered) != dest or version_key(oldversion) > version_key(VERSION)):
        raise ValueError("请选择已登记的安装目录及相同或更新版本")
    if not registered and ((dest.exists() and any(dest.iterdir())) or link.exists() or oldreg):
        raise ValueError("所选目录或快捷方式已有其他内容，请选择空目录")

    new = json.loads((package / "payload.json").read_text("utf-8"))
    old = {}
    if registered:
        source = package / "legacy.json" if oldversion == LEGACY else child(dest, INDEX)
        old = json.loads(source.read_text("utf-8"))
    new["Uninstall.exe"] = digest(stage / "Uninstall.exe")
    old_names = set(old)
    if registered:
        old_names.add("Uninstall.exe")
        if oldversion != LEGACY:
            old_names.add(INDEX)
    for name, expected in new.items():
        if digest(child(stage, name)) != expected:
            raise ValueError(f"安装材料损坏：{name}")
    (stage / INDEX).write_text(json.dumps(new, ensure_ascii=False), encoding="utf-8")
    new_names = set(new) | {INDEX}
    files = sorted(old_names | new_names)
    fresh = []
    for name in files:
        target = child(dest, name)
        if target.exists():
            if not target.is_file() or name not in old_names:
                raise ValueError(f"已有用户文件与程序同名：{name}")
            if name in old and digest(target) != old[name]:
                raise ValueError(f"已安装文件有外部修改，请先保留该文件：{name}")
        else:
            fresh.append(name)
    dest.mkdir(parents=True, exist_ok=True)
    plan = dict(
        product=marker,
        dest=str(dest),
        link=str(link),
        registry=oldreg,
        had_link=link.exists(),
        files=files,
        fresh=fresh,
    )
    # Publish a complete journal before touching any installed file. A crash while
    # preparing the journal can then be retried without an incomplete transaction.
    with tempfile.TemporaryDirectory(dir=dest.parent, prefix=".LabControl-prepare-") as staging:
        prepared = Path(staging) / "journal"
        prepared.mkdir()
        if link.exists():
            shutil.copy2(link, prepared / "old.lnk")
        (prepared / journal.name).write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        make_shortcut(prepared / "new.lnk", dest / "LabControl.exe")
        os.rename(prepared, txn)
    try:
        for name in files:
            target = child(dest, name)
            if target.exists():
                backup = child(txn / "old", name)
                backup.parent.mkdir(parents=True, exist_ok=True)
                os.replace(target, backup)
            if name in new_names:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(stage / name, target)
        # Empty versioned .dist-info directories can shadow the newly installed
        # package metadata. Remove only obsolete, empty program directories.
        old_dirs = {p for name in old_names for p in Path(name).parents}
        new_dirs = {p for name in new_names for p in Path(name).parents}
        for rel in sorted(old_dirs - new_dirs, key=lambda p: len(p.parts), reverse=True):
            try:
                child(dest, str(rel)).rmdir()
            except FileNotFoundError:
                pass
            except OSError as exc:
                if exc.errno != errno.ENOTEMPTY:
                    raise
        programs.mkdir(parents=True, exist_ok=True)
        shutil.copy2(txn / "new.lnk", link)
        entries = dict(
            DisplayName="LabControl 石墨版",
            DisplayVersion=VERSION,
            Publisher="LabControl",
            InstallLocation=str(dest),
            DisplayIcon=str(dest / "LabControl.exe"),
            UninstallString=f'"{dest / "Uninstall.exe"}"',
            QuietUninstallString=f'"{dest / "Uninstall.exe"}" /S',
        )
        write_registration(
            {
                **oldreg,
                **{k: [v, winreg.REG_SZ] for k, v in entries.items()},
                "NoModify": [1, winreg.REG_DWORD],
                "NoRepair": [1, winreg.REG_DWORD],
            }
        )
        (txn / "committed").touch()
    except BaseException:
        if journal.exists():
            rollback(dest, txn, plan, link)
        discard_transaction(txn, dest)
        raise
    discard_transaction(txn, dest)


def main(args):
    stage, dest, programs, package = map(Path, args)
    try:
        with installation_lock():
            install(stage, dest, programs, package)
    except Exception as exc:
        (package / "error.txt").write_text(str(exc), encoding="utf-16")
        log = Path(os.environ["LOCALAPPDATA"]) / "LabControl/maintenance/last-software-install.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(traceback.format_exc(), encoding="utf-8")
        return 1
    return 0
