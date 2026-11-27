"""Wrap a verified Windows onedir bundle with the per-user Graphite installer."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from collections import defaultdict
from pathlib import Path

from lab_control.deployment.software_update import VERSION, Release

ROOT = Path(__file__).resolve().parents[1]


def nsis_path(path):
    text = str(path)
    if any(c in text for c in ('"', "\r", "\n")):
        raise ValueError(f"Invalid NSIS file path: {path}")
    return text.replace("$", "$$")


def main():
    args = argparse.ArgumentParser(description=__doc__)
    args.add_argument("bundle", type=Path)
    args.add_argument("output", type=Path)
    args.add_argument("--compiler", type=Path)
    options = args.parse_args()
    bundle = options.bundle.resolve(strict=True)
    if not (bundle / "LabControl.exe").is_file() or not (bundle / "_internal").is_dir():
        args.error("Expected the complete Windows directory bundle")
    metadata = json.loads(
        (bundle / "_internal/lab_control/deployment/release-manifest.json").read_text("utf-8")
    )
    if metadata.get("software_version") != VERSION or metadata.get("target") != "windows-x64":
        args.error("Bundle version or architecture does not match this installer")
    default_compiler = (
        Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")) / "NSIS/makensis.exe"
    )
    compiler = (
        options.compiler
        or shutil.which("makensis")
        or (
            default_compiler
            if default_compiler.is_file()
            else Path(os.environ["LOCALAPPDATA"]) / "tauri/NSIS/makensis.exe"
        )
    )
    if subprocess.check_output([str(compiler), "/VERSION"]).strip() != b"v3.13":
        args.error("The inventoried NSIS compiler is v3.13")
    options.output.mkdir(parents=True, exist_ok=True)
    setup = options.output.resolve() / Release(VERSION, "", 0, "").filename
    if setup.exists():
        args.error("Output exists; choose a new build directory")
    temp = ROOT / ".temp"
    temp.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=temp, prefix="setup-") as folder:
        generated = Path(folder)
        files = defaultdict(list)
        payload = {}
        for path in sorted(bundle.rglob("*")):
            if not path.is_file():
                continue
            if path.parent.name.endswith(".dist-info") and path.name in {
                "direct_url.json",
                "uv_build.json",
                "uv_cache.json",
            }:
                continue
            rel = path.relative_to(bundle)
            if path.is_symlink() or path.suffix.lower() in {".sqlite3", ".db", ".log"}:
                raise ValueError(f"Unexpected runtime data: {rel}")
            with path.open("rb") as stream:
                payload[str(rel)] = hashlib.file_digest(stream, "sha256").hexdigest()
            files[rel.parent].append(path)
        install, remove = [], []
        for sub, paths in files.items():
            suffix = "" if sub == Path(".") else "\\" + nsis_path(sub)
            install.append(f'SetOutPath "$PLUGINSDIR\\payload{suffix}"')
            for path in paths:
                install += [f'File "{nsis_path(path)}"', "IfErrors failed"]
                remove.append('Delete "${ROOT}\\' + nsis_path(path.relative_to(bundle)) + '"')
        dirs = {parent for name in payload for parent in Path(name).parents if parent != Path(".")}
        remove.extend(
            'RMDir "${ROOT}\\' + nsis_path(p) + '"' for p in sorted(dirs, key=lambda p: (-len(p.parts), p))
        )
        for name, lines in (
            ("install-files", install),
            ("remove-files", ["!macro RemoveOwnedFiles ROOT", *remove, "!macroend"]),
        ):
            (generated / (name + ".nsh")).write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
        (generated / "payload.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        subprocess.run(
            [
                str(compiler),
                "/V2",
                "/INPUTCHARSET",
                "UTF8",
                f"/DOUT={setup}",
                f"/DICON={ROOT / 'src/lab_control/ui/styles/sciagent.ico'}",
                f"/DVERSION={VERSION}",
                f"/DWINVERSION={VERSION}.0",
                f"/DGENERATED={generated}",
                f"/DLEGACY={ROOT / 'deployment/installer/legacy-graphite.1.json'}",
                str(ROOT / "deployment/installer/setup.nsi"),
            ],
            check=True,
        )
    with setup.open("rb") as stream:
        sha = hashlib.file_digest(stream, "sha256").hexdigest()
    (setup.parent / "SHA256SUMS.txt").write_text(f"{sha}  {setup.name}\n", encoding="ascii")
    print(setup)


if __name__ == "__main__":
    main()
