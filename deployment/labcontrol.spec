# Reproducible Windows x64 directory build. No vendor DLLs are copied from the host.
import os
import sys
import sysconfig
import tomllib
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

root = Path(SPECPATH).parent
datas = collect_data_files("lab_control", includes=["ui/styles/*.qss", "ui/styles/*.svg", "agent/*.ps1", "instruments/devices/**/*.md", "instruments/devices/**/tests/fixtures/*.json"])
icon = root / "src/lab_control/ui/styles/sciagent.ico"
datas += [(str(icon), "lab_control/ui/styles")]
datas += [(str(root / "deployment/components.json"), "lab_control/deployment")]
datas += [(os.environ.get("LABCONTROL_BUILD_MANIFEST", str(root / "build/deployment/release-manifest.json")), "lab_control/deployment")]
# Use the installed wheel metadata, even when an older editable egg-info exists under src.
version = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
metadata = Path(sysconfig.get_path("purelib")) / f"labcontrol-{version}.dist-info" / "METADATA"
datas += [(str(metadata), metadata.parent.name)]
for distribution in ("PySide6", "PyVISA", "pyserial", "numpy", "pyqtgraph", "shiboken6", "PySide6_Essentials", "PySide6_Addons"):
    datas += copy_metadata(distribution)
datas += copy_metadata("zeroconf", recursive=True)
datas += copy_metadata("moku", recursive=True)
datas += copy_metadata("mcp", recursive=True)
datas += copy_metadata("cryptography", recursive=True)
hidden = collect_submodules("lab_control", filter=lambda name: ".tests" not in name and ".hardware" not in name)
hidden += collect_submodules("zeroconf")
hidden += ["moku.instruments"]
hidden += ["uvicorn.logging", "uvicorn.loops.asyncio", "uvicorn.protocols.http.h11_impl", "uvicorn.lifespan.on"]
a = Analysis([str(root / "deployment/entry.py")], pathex=[str(root / "src")], datas=datas,
             hiddenimports=hidden, excludes=["pytest", "ruff", "mypy", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets"])
# Analysis may also discover an editable install left by a previous wheel build.
# Keep one authoritative project metadata file in the frozen application.
a.datas = [
    entry for entry in a.datas
    if Path(entry[0]).parts[0] != "labcontrol.egg-info"
    and (Path(entry[0]).parts[0] != metadata.parent.name or Path(entry[0]).name == "METADATA")
]
# Reject accidental dependencies supplied by other software on the build machine.
allowed = tuple(Path(p).resolve() for p in (sys.prefix, sys.base_prefix, os.environ['WINDIR']))
for destination, source, kind in a.binaries:
    if not any(Path(source).resolve().is_relative_to(folder) for folder in allowed):
        raise ValueError(f"Unregistered native build dependency: {source}")
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="LabControl", debug=False,
          icon=str(icon), bootloader_ignore_signals=False, strip=False, upx=False, console=False)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="LabControl")
