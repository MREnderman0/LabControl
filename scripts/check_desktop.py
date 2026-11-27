"""Check an isolated bundle under .temp with real frozen workers and a Mock GUI."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import shutil
import struct
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def verify_exe_icons(bundle: Path) -> list[str]:
    """Compare every icon image referenced by each Windows icon group with the source ICO."""
    import pefile

    ico = (ROOT / "src/lab_control/ui/styles/sciagent.ico").read_bytes()
    count = struct.unpack_from("<H", ico, 4)[0]
    images = []
    for index in range(count):
        size, offset = struct.unpack_from("<II", ico, 6 + index * 16 + 8)
        images.append(ico[offset : offset + size])
    name = "LabControl.exe"
    assert {p.name for p in bundle.glob("*.exe")} == {name}
    with pefile.PE(str(bundle / name), fast_load=True) as pe:
        pe.parse_data_directories([pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_RESOURCE"]])
        types = {t.id: t.directory.entries for t in pe.DIRECTORY_ENTRY_RESOURCE.entries}

        def resource(entry):
            data = entry.directory.entries[0].data.struct
            return pe.get_data(data.OffsetToData, data.Size)

        resources = {entry.id: resource(entry) for entry in types[3]}
        assert types[14], name
        for group in types[14]:
            data = resource(group)
            assert struct.unpack_from("<HHH", data) == (0, 1, count), name
            referenced = [struct.unpack_from("<H", data, 6 + 14 * i + 12)[0] for i in range(count)]
            assert [resources[i] for i in referenced] == images, name
    return [name]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, default=ROOT / "dist/LabControl")
    parser.add_argument("--report", type=Path, default=ROOT / ".temp/deployment/frozen-smoke.json")
    parser.add_argument(
        "--check-agent-default",
        action="store_true",
        help="Verify explicit MCP mode before GUI startup",
    )
    args = parser.parse_args()
    args.report = args.report.resolve()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    scratch = ROOT / ".temp"
    scratch.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="frozen-中文 ", dir=scratch) as temporary:
        outside = Path(temporary).resolve()
        assert outside.parent == scratch.resolve()
        target = outside / "应用 程序"
        shutil.copytree(args.bundle.resolve(), target)
        executable = target / "LabControl.exe"
        environment = dict(os.environ)
        for name in ("PYTHONPATH", "PYTHONHOME", "QT_PLUGIN_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH"):
            environment.pop(name, None)
        windows = Path(environment.get("WINDIR", r"C:\Windows"))
        environment.update(
            PATH=os.pathsep.join(
                map(str, (windows / "System32", windows, windows / "System32/WindowsPowerShell/v1.0"))
            ),
            QT_QPA_PLATFORM="offscreen",
            LAB_CONTROL_HOME=str(outside / "data"),
            LOCALAPPDATA=str(outside / "local"),
        )
        if args.check_agent_default:
            connector = subprocess.run(
                [str(executable), "--agent-connector"],
                cwd=outside,
                env=environment,
                capture_output=True,
                timeout=20,
            )
            assert connector.returncode == 2 and b"--connection" in connector.stderr
            assert not connector.stdout and not list(outside.rglob("hardware.lock"))
        result = subprocess.run(
            [str(executable), "--deployment-check", str(args.report)],
            cwd=outside,
            env=environment,
            capture_output=True,
            timeout=90,
        )
        if result.returncode:
            raise RuntimeError(
                args.report.read_text(encoding="utf-8") if args.report.exists() else repr(result.stderr)
            )
        folder = outside / "损坏的记录 中文"
        folder.mkdir()
        data = folder / "segment-00001.sqlite3"
        data.write_bytes(b"invalid sqlite fixture")
        worker = subprocess.run(
            [str(executable), "--csv-worker", str(folder)],
            cwd=outside,
            env=environment,
            capture_output=True,
            timeout=20,
        )
        assert worker.returncode == 1
        assert data.read_bytes() == b"invalid sqlite fixture"
        assert json.loads((folder / "csv-status.json").read_text())["error"]
        assert not list(outside.rglob("hardware.lock"))
        report = json.loads(args.report.read_text(encoding="utf-8"))
        versions = [
            dist.version
            for dist in importlib.metadata.distributions(name="labcontrol", path=[str(target / "_internal")])
        ]
        assert versions == [report["software_version"]], f"Ambiguous frozen LabControl metadata: {versions}"
        report.update(
            frozen_project_versions=versions,
            exe_icons=verify_exe_icons(target),
            outside_source=False,
            isolated_bundle=True,
            path_sanitized=True,
            corrupt_worker_preserved_input=True,
            connector_without_arguments_requires_connection=args.check_agent_default,
            exe_sha256=hashlib.sha256(executable.read_bytes()).hexdigest(),
        )
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
