import copy
import hashlib
import json
import runpy
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from lab_control.application.config import ConnectionConfig
from lab_control.deployment import environment
from lab_control.deployment.catalog import load_manifest, payload_status, release_issues, required_components
from lab_control.launcher import worker_command

ROOT = Path(__file__).resolve().parents[2]


def test_builder_checks_transitive_versions_and_platform_markers(monkeypatch):
    builder = runpy.run_path(str(ROOT / "scripts/build_desktop.py"))
    lock = {
        "package": [
            {
                "name": "root",
                "version": "1",
                "dependencies": [
                    {"name": "Child_Name"},
                    {"name": "foreign", "marker": "sys_platform == 'darwin'"},
                ],
            },
            {"name": "child-name", "version": "2", "dependencies": [{"name": "root"}]},
            {"name": "foreign", "version": "3"},
        ]
    }
    versions = {"root": "1", "child-name": "2"}
    monkeypatch.setattr("importlib.metadata.version", versions.__getitem__)
    verify = builder["verified_packages"]
    assert verify(lock, ["Root"], environment={"sys_platform": "win32"}) == [
        {"name": "child-name", "version": "2"},
        {"name": "root", "version": "1"},
    ]
    versions["child-name"] = "9"
    with pytest.raises(ValueError, match="child-name installed 9, lock requires 2"):
        verify(lock, ["root"], environment={"sys_platform": "win32"})


def test_builder_rejects_missing_or_ambiguous_dependencies(monkeypatch):
    import importlib.metadata

    verify = runpy.run_path(str(ROOT / "scripts/build_desktop.py"))["verified_packages"]
    lock = {"package": [{"name": "root", "version": "1"}]}

    def missing(name):
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", missing)
    with pytest.raises(ValueError, match="not installed: root"):
        verify(lock, ["root"])
    with pytest.raises(ValueError, match="found 0"):
        verify(lock, ["unknown"])
    lock["package"].append({"name": "root", "version": "2"})
    with pytest.raises(ValueError, match="found 2"):
        verify(lock, ["root"])


def test_inventory_and_runtime_import_coverage():
    check = runpy.run_path(str(ROOT / "scripts/check_deployment.py"))["check"]
    result = check()
    assert result["passed"], result
    assert result["pending"]


def test_new_runtime_dependency_cannot_bypass_registration(tmp_path):
    (tmp_path / "deployment").mkdir()
    (tmp_path / "deployment/components.json").write_text(json.dumps(load_manifest()))
    (tmp_path / "pyproject.toml").write_text('[project]\ndependencies=["requests>=2"]\n')
    (tmp_path / "uv.lock").write_text('package=[{name="shiboken6"}]')
    source = tmp_path / "src/lab_control"
    source.mkdir(parents=True)
    (source / "new.py").write_text("import requests\n")
    result = runpy.run_path(str(ROOT / "scripts/check_deployment.py"))["check"](tmp_path)
    assert not result["passed"]
    assert any("requests" in error for error in result["errors"])


def test_build_does_not_collect_native_libraries_from_other_applications(monkeypatch):
    monkeypatch.setenv("PATH", "C:\\UnrelatedApp\\bin")
    monkeypatch.setenv("PYTHONPATH", "C:\\OtherSource")
    monkeypatch.setenv("QT_PLUGIN_PATH", "C:\\OtherQt")
    environment = runpy.run_path(str(ROOT / "scripts/build_desktop.py"))["build_environment"]()
    assert "UnrelatedApp" not in environment["PATH"]
    assert "PYTHONPATH" not in environment
    assert "QT_PLUGIN_PATH" not in environment


def test_unknown_payload_install_method_and_redistribution_block_release(tmp_path):
    component = load_manifest()["components"][0]
    problems = release_issues(component, (tmp_path,))
    assert "offline payload missing" in problems
    assert "redistribution not verified" in problems
    assert "installation arguments/exit codes pending" in problems
    incomplete = copy.deepcopy(component)
    incomplete["architecture"] = "x86"
    from lab_control.deployment.catalog import validate_manifest

    assert any("architecture" in e for e in validate_manifest({"components": [incomplete]}))


def test_payload_hash_missing_corrupt_valid_and_cancel(tmp_path):
    component = {"filename": "installer.exe", "sha256": hashlib.sha256(b"fixture").hexdigest()}
    assert payload_status(component, (tmp_path,))["state"] == "missing"
    path = tmp_path / component["filename"]
    path.write_bytes(b"bad")
    assert payload_status(component, (tmp_path,))["state"] == "invalid"
    path.write_bytes(b"fixture")
    assert payload_status(component, (tmp_path,))["state"] == "verified"
    cancel = threading.Event()
    cancel.set()
    assert payload_status(component, (tmp_path,), cancel)["state"] == "cancelled"


def test_model_refs_and_unused_optional_components():
    refs = required_components((ConnectionConfig("ASRL3::INSTR", "gwinstek.gpd4303s"),))
    assert {"rs-visa", "ftdi-vcp"} <= refs
    assert "pico-sdk" not in refs
    refs = required_components((ConnectionConfig("tcp://example.invalid:5025", "rs.sma100b", "tcp"),))
    assert "rs-visa" not in refs


def test_inspection_never_opens_visa_or_devices_and_preserves_partial_result(tmp_path, monkeypatch):
    import pyvisa

    monkeypatch.setattr(pyvisa, "ResourceManager", lambda *a: pytest.fail("VISA manager opened"))
    monkeypatch.setattr("lab_control.deployment.windows.inspect_windows", lambda cancel: {})
    cancel = threading.Event()
    seen = []

    def progress(report):
        seen.append(report)
        if len(report["items"]) == 3:
            cancel.set()

    report = environment.inspect_environment(tmp_path, cancel=cancel, progress=progress, caches=(tmp_path,))
    assert report["state"] == "cancelled"
    assert len(report["items"]) == 3
    assert not report["repair_plan"]["executed"]
    assert not report["repair_plan"]["inspection_complete"]
    assert not list(tmp_path.iterdir())


def test_inspection_reports_optional_driver_unknown_and_storage_failure(tmp_path, monkeypatch):
    monkeypatch.setattr("lab_control.deployment.windows.inspect_windows", lambda cancel: {})
    monkeypatch.setattr(environment, "_storage", lambda root: (_ for _ in ()).throw(OSError("read-only")))
    monkeypatch.setattr(environment, "expected_versions", lambda: {"numpy": "0.0"})
    report = environment.inspect_environment(tmp_path, caches=(tmp_path,))
    by_id = {item["id"]: item for item in report["items"]}
    assert not by_id["pico-sdk"]["required"]
    assert by_id["pico-sdk"]["payload"]["state"] == "missing"
    assert by_id["storage"]["state"] == "error"
    assert by_id["msvc-runtime"]["state"] == "unknown"
    assert by_id["python:numpy"]["state"] == "incompatible"
    assert "pytest" not in by_id
    path = tmp_path / "中文 report.json"
    environment.export_report(report, path)
    assert json.loads(path.read_text(encoding="utf-8")) == report


def test_inspection_reports_missing_runtime_in_model_search_path(tmp_path, monkeypatch):
    from lab_control.application.config import ConnectionConfig

    monkeypatch.setattr("lab_control.deployment.windows.inspect_windows", lambda cancel: {})
    monkeypatch.setattr(
        "lab_control.instruments.devices.kplusk_fxe.adapter.runtime_directories", lambda: (tmp_path,)
    )
    report = environment.inspect_environment(
        tmp_path,
        (ConnectionConfig("192.0.2.10", "kplusk.fxe", "kplusk"),),
        caches=(tmp_path,),
    )
    by_id = {item["id"]: item for item in report["items"]}
    assert report["state"] == "complete"
    assert by_id["kplusk-library-win"]["state"] == "missing"
    assert "KK_FX80E.dll" in by_id["kplusk-library-win"]["reason"]
    assert "kklib.py" in by_id["kplusk-python-wrapper"]["reason"]


@pytest.mark.parametrize(
    ("version", "expected"),
    [("v14.51.36247.0", "ok"), ("v14.34.31823.03", "incompatible")],
)
def test_vc_runtime_detection_never_accepts_an_older_version(tmp_path, monkeypatch, version, expected):
    monkeypatch.setattr(
        "lab_control.deployment.windows.inspect_windows",
        lambda cancel: {"vc_runtime": {"Installed": 1, "Version": version}},
    )
    report = environment.inspect_environment(tmp_path, caches=(tmp_path,))
    item = next(item for item in report["items"] if item["id"] == "msvc-runtime")
    assert item["state"] == expected


def test_frozen_worker_command_and_dispatch_before_qt(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    command = worker_command(tmp_path)
    assert command == [sys.executable, "--csv-worker", str(tmp_path.resolve())]


def test_source_worker_real_process_handles_unicode_path_without_hardware(tmp_path):
    target = tmp_path / "中文 folder"
    target.mkdir()
    # Corrupt committed input must fail visibly, preserving its bytes.
    import numpy as np

    from lab_control.domain.instruments import DeviceIdentity, Measurement, MeasurementBatch
    from lab_control.storage.recording import SegmentWriter

    writer = SegmentWriter(target)
    path = Path(
        writer.append(
            1,
            MeasurementBatch(DeviceIdentity("Fixture", "CSV"), (Measurement("test", "V", np.arange(4)),), {}),
        )
    )
    writer.close()
    result = subprocess.run(worker_command(target), cwd=tmp_path, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert json.loads((target / "csv-status.json").read_text())["converted"] == 1
    assert path.with_suffix(".csv").exists()
    assert not list(tmp_path.rglob("hardware.lock"))


def test_source_worker_failure_is_reported_and_does_not_delete_input(tmp_path):
    path = tmp_path / "segment-000001.sqlite3"
    path.write_bytes(b"broken sqlite fixture")
    failed = subprocess.run(worker_command(tmp_path), capture_output=True, timeout=15)
    assert failed.returncode == 1
    assert json.loads((tmp_path / "csv-status.json").read_text())["error"]
    result = subprocess.run(worker_command(tmp_path / "missing"), capture_output=True, timeout=15)
    assert result.returncode == 2
    assert path.read_bytes() == b"broken sqlite fixture"


def test_sdk_reports_every_configured_route_and_independent_model(tmp_path, monkeypatch):
    calls = []

    def diagnostics(configs, *, sdk_kinds):
        calls.append(set(sdk_kinds))
        return {
            "sdk": {
                kind: {"state": "missing" if kind == "pm100d" else "loadable", "libraries": []}
                for kind in sdk_kinds
            }
        }

    monkeypatch.setattr("lab_control.application.environment.dependency_diagnostics", diagnostics)
    monkeypatch.setattr("lab_control.deployment.windows.inspect_windows", lambda cancel: {})
    configs = tuple(
        ConnectionConfig("fixture", adapter, kind)
        for adapter, kind in (
            ("thorlabs.pm100d", "tlpmx"),
            ("thorlabs.pm100d", "pm100d"),
            ("pico.6404e", "pico"),
        )
    )
    report = environment.inspect_environment(tmp_path, configs, caches=(tmp_path,))
    items = {row["id"]: row for row in report["items"]}
    assert {"pm100d", "tlpmx"} in calls and {"pico"} in calls
    assert items["thorlabs-opm"]["state"] == "missing" and items["thorlabs-opm"]["required"]
    assert items["pico-sdk"]["state"] == "ok"
    assert items["pico-sdk"]["payload"]["state"] == "missing"
    assert {c["id"] for c in load_manifest()["components"]} <= items.keys()


def test_import_and_icon_damage_survive_metadata(tmp_path, monkeypatch):
    import lab_control.app

    original = environment.importlib.import_module

    def broken(name, *args, **kwargs):
        if name == "serial":
            raise ImportError("missing package file")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(environment.importlib, "import_module", broken)
    monkeypatch.setattr(lab_control.app, "ICON_PATH", tmp_path / "broken.ico")
    monkeypatch.setattr("lab_control.deployment.windows.inspect_windows", lambda cancel: {})
    (tmp_path / "broken.ico").write_bytes(b"invalid")
    items = {i["id"]: i for i in environment.inspect_environment(tmp_path, caches=(tmp_path,))["items"]}
    assert items["python:pyserial"]["state"] == items["resources"]["state"] == "error"


def test_ni_runtime_reports_missing_export_without_driver_calls(tmp_path, monkeypatch):
    from lab_control.instruments.devices.ni_9230 import daqmx

    path = tmp_path / "nicaiu.dll"
    path.touch()
    monkeypatch.setattr(daqmx, "runtime_path", lambda: path)
    monkeypatch.setattr("lab_control.application.environment.pe_bits", lambda _: 64)
    monkeypatch.setattr(daqmx.ct, "WinDLL", lambda _: object(), raising=False)
    state, reason = daqmx.inspect_component({})
    assert state == "incompatible" and "DAQmx" in reason


def test_windows_multiple_interfaces_and_service_path(monkeypatch):
    from lab_control.deployment import windows

    components = {c["id"]: c for c in load_manifest()["components"]}
    monkeypatch.setattr(windows, "driver_packages", lambda names: {})
    drivers = [
        {
            "DeviceID": "USB/" + c["detection"]["hardware_ids"][0],
            "InfName": "oem4.inf",
            "IsSigned": True,
            "DriverVersion": c["version"],
        }
        for c in (components["ftdi-vcp"], components["silabs-cp210x"])
    ]
    observations = {"drivers": drivers}
    for key in ("ftdi-vcp", "silabs-cp210x"):
        assert windows.inspect_serial(components[key], observations)[0] == "ok"
    assert windows.inspect_serial(components["wch-ch34x"], observations)[0] == "missing"
    svc = {
        "Name": "tftpd64",
        "PathName": "fixture/tftpd64.exe",
        "Binary": "fixture/tftpd64.exe",
        "Version": "4.74.0.0",
    }
    assert windows.inspect_dhcp(components["tftpd64"], {"services": [svc]})[0] == "ok"
    svc["Version"] = None
    assert windows.inspect_dhcp(components["tftpd64"], {"services": [svc]})[0] == "unknown"


def test_common_and_explicit_model_materials_share_concrete_closure():
    from lab_control.deployment.catalog import default_components, model_components
    from lab_control.deployment.materials import component_closure

    manifest = load_manifest()
    components = {c["id"]: c for c in manifest["components"]}
    common = set(default_components(manifest))
    assert len(components) == 19 and len(common) == 9
    assert all(components[key]["scope"] == "common" for key in common)
    assert {c["id"] for c in component_closure(["serial-interface", "gpib-interface"])} == common - {
        "msvc-runtime",
        "rs-visa",
        "tftpd64",
    }
    assert model_components(["ni.9230", "pico.6404e"]) == ["ni-daqmx", "pico-sdk"]
    assert [c["id"] for c in component_closure(["kplusk-python-wrapper"])] == [
        "kplusk-library-win",
        "kplusk-python-wrapper",
    ]


def test_kplusk_split_runtime_files_and_first_complete_directory(tmp_path, monkeypatch):
    from lab_control.instruments.devices.kplusk_fxe import adapter

    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    monkeypatch.setattr(adapter, "runtime_directories", lambda: (first, second))
    manifest = {c["id"]: copy.deepcopy(c) for c in load_manifest()["components"]}
    component = manifest["kplusk-python-wrapper"]
    for c in (component, manifest["kplusk-library-win"]):
        for m in c["archive_members"]:
            m.update(sha256=hashlib.sha256(b"ok").hexdigest(), size=2)
    (first / "kklib.py").write_bytes(b"ok")
    for name in ("KK_FX80E.dll", "KK_Library_64.dll"):
        (second / name).write_bytes(b"ok")
    assert adapter.inspect_component(component)[0] == "unknown"
    assert adapter.inspect_component(manifest["kplusk-library-win"])[0] == "ok"
    (second / "kklib.py").write_bytes(b"bad")
    assert adapter.inspect_component(component)[0] == "unknown"
    (second / "kklib.py").write_bytes(b"ok")
    assert adapter.inspect_component(component)[0] == "ok"
