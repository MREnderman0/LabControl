import json
import os
from importlib.metadata import distributions
from pathlib import Path

import pytest


@pytest.mark.skipif(os.name != "nt", reason="Windows per-user installer")
def test_upgrade_removes_obsolete_metadata_and_preserves_user_files(tmp_path, monkeypatch):
    from lab_control.deployment import installer

    stage, dest, package, programs = (tmp_path / name for name in ("stage", "installed", "setup", "menu"))
    for folder in (stage, dest, package, programs):
        folder.mkdir()
    old_info = Path("_internal/labcontrol-0.3.0.dist-info/METADATA")
    new_info = Path(f"_internal/labcontrol-{installer.VERSION}.dist-info/METADATA")
    old_program = Path("_internal/old-driver/module.py")
    old_files = {old_info: b"Name: labcontrol\nVersion: 0.3.0\n", old_program: b"old"}
    for rel, data in old_files.items():
        path = dest / rel
        path.parent.mkdir(parents=True)
        path.write_bytes(data)
    user_file = (dest / old_program).with_name("notes.txt")
    user_file.write_text("用户校准", encoding="utf-8")
    (stage / new_info).parent.mkdir(parents=True)
    (stage / new_info).write_text(f"Name: labcontrol\nVersion: {installer.VERSION}\n", encoding="utf-8")
    (stage / "LabControl.exe").write_bytes(b"new")
    (stage / "Uninstall.exe").write_bytes(b"remove")
    old = {str(rel): installer.digest(dest / rel) for rel in old_files}
    new = {str(rel): installer.digest(stage / rel) for rel in (new_info, Path("LabControl.exe"))}
    (package / "legacy.json").write_text(json.dumps(old), encoding="utf-8")
    (package / "payload.json").write_text(json.dumps(new), encoding="utf-8")
    registered = {"InstallLocation": [str(dest), 1], "DisplayVersion": [installer.LEGACY, 1]}
    monkeypatch.setattr(installer, "registration", lambda: registered)
    monkeypatch.setattr(installer, "write_registration", lambda values: registered.update(values))
    monkeypatch.setattr(
        installer, "make_shortcut", lambda path, target: path.write_text(str(target), "utf-8")
    )

    installer.install(stage, dest, programs, package)

    versions = [d.version for d in distributions(path=[str(dest / "_internal")])]
    assert versions == [installer.VERSION]
    assert not (dest / old_info).parent.exists()
    assert user_file.read_text(encoding="utf-8") == "用户校准"
    assert registered["DisplayVersion"][0] == installer.VERSION
