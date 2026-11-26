"""Bounded, read-only Windows observations; never install, scan or reconfigure."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path


def powershell(script: str, *, cancel=None, timeout: float = 20) -> dict:
    if sys.platform != "win32":
        raise OSError("Windows inspection is unavailable on this platform")
    prefix = "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; $ErrorActionPreference='Stop'; "
    environment = dict(os.environ)
    windows = Path(environment.get("WINDIR", r"C:\Windows"))
    # A PowerShell 7 parent can leak incompatible Core modules into Windows PowerShell 5.1.
    environment["PSModulePath"] = os.pathsep.join(
        map(
            str,
            (
                windows / "System32/WindowsPowerShell/v1.0/Modules",
                Path(environment.get("ProgramFiles", r"C:\Program Files")) / "WindowsPowerShell/Modules",
            ),
        )
    )
    process = subprocess.Popen(
        [
            str(windows / "System32/WindowsPowerShell/v1.0/powershell.exe"),
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            prefix + script,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8-sig",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        env=environment,
    )
    deadline = time.monotonic() + timeout
    try:
        while True:
            if cancel and cancel.is_set():
                raise InterruptedError("检查已取消")
            if time.monotonic() >= deadline:
                raise TimeoutError("Windows inspection timed out")
            try:
                stdout, stderr = process.communicate(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                continue
        if process.returncode:
            raise OSError(stderr.strip() or f"Windows inspection exited {process.returncode}")
        return json.loads(stdout)
    finally:
        if process.poll() is None:
            process.terminate()  # Only this inspection's own child, never a vendor process.
            process.communicate()


def signature_status(path: Path) -> dict:
    # Pass path as a single-quoted PowerShell literal, with embedded quotes doubled.
    literal = "'" + str(path.resolve()).replace("'", "''") + "'"
    try:
        return powershell(
            f"$s=Get-AuthenticodeSignature -LiteralPath {literal}; "
            "@{status=$s.Status.ToString();subject=$s.SignerCertificate.Subject}|ConvertTo-Json -Compress"
        )
    except (OSError, ValueError, TimeoutError) as exc:
        return {"status": "unknown", "error": str(exc)}


def inspect_windows(cancel=None) -> dict:
    return powershell(
        r"""
$result=@{}
try { $result.drivers=@(Get-CimInstance Win32_PnPSignedDriver | Where-Object {
    $_.DeviceClass -in @('PORTS','USB','GPIB','INSTRUMENT') -or
    $_.Manufacturer -match 'FTDI|Pico|Thorlabs|Keithley|National Instruments'
} | Select-Object DeviceName,DeviceID,HardwareID,Manufacturer,DriverVersion,InfName,IsSigned) }
catch { $result.drivers_error=$_.Exception.Message }
try { $result.services=@(Get-CimInstance Win32_Service | Where-Object {
    $_.Name -match 'tftpd|tserve|labcontrol' -or $_.DisplayName -match 'tftpd|tserve|labcontrol'
} | ForEach-Object {
    $path=[Environment]::ExpandEnvironmentVariables($_.PathName)
    $version=$null; $binary=$null
    if ($path -match '^\s*"?(.+?\.exe)"?(?:\s|$)') {
        $binary=$Matches[1]
        if (Test-Path -LiteralPath $binary -PathType Leaf) {
            $version=(Get-Item -LiteralPath $binary).VersionInfo.FileVersion
        }
    }
    [pscustomobject]@{Name=$_.Name;State=$_.State;PathName=$_.PathName;Binary=$binary;Version=$version}
}) }
catch { $result.services_error=$_.Exception.Message }
try { $result.products=@(Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*',
    'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*',
    'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*' -ErrorAction SilentlyContinue |
    Where-Object { $_.DisplayName -match 'VISA|488\.2|IO Libraries|Moku' } |
    Select-Object DisplayName,DisplayVersion,InstallLocation,Publisher) }
catch { $result.products_error=$_.Exception.Message }
try { $result.firewall_profiles=@(Get-NetFirewallProfile | Select-Object Name,Enabled,DefaultInboundAction,DefaultOutboundAction)
    $result.firewall_rules=@(Get-NetFirewallRule | Where-Object {$_.Group -in @('LabControl','LabControl-Agent','TF-LabControl','TF-LabControl-Agent')} |
        Select-Object Name,Enabled,Direction,Action,Profile)
} catch { $result.firewall_error=$_.Exception.Message }
try { $result.vc_runtime=Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64' |
    Select-Object Installed,Version }
catch { $result.vc_runtime_error=$_.Exception.Message }
$result | ConvertTo-Json -Depth 6 -Compress
""",
        cancel=cancel,
    )


def driver_packages(names: list[str]) -> dict[str, list[str]]:
    """Read the version from matching x64 DriverStore INFs without elevation."""
    root = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/DriverStore/FileRepository"
    if not root.is_dir():
        raise FileNotFoundError(root)
    packages: dict[str, list[str]] = {}
    for name in names:
        for path in root.glob(name + "_amd64_*/" + name):
            raw = path.read_bytes()
            encoding = "utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
            match = re.search(
                r"(?im)^\s*DriverVer\s*=\s*[^,\n]+,\s*([\d.]+)", raw.decode(encoding, "replace")
            )
            packages.setdefault(name, []).append(match[1] if match else "版本字段缺失")
    return packages


def inspect_serial(component: dict, windows: dict) -> tuple[str, str]:
    detection = component["detection"]
    bound = []
    for row in windows.get("drivers") or []:
        ids = str(row.get("HardwareID", "")) + " " + str(row.get("DeviceID", ""))
        if any(pattern in ids.upper() for pattern in detection["hardware_ids"]):
            bound.append(row)
    try:
        packages = driver_packages(detection["infs"])
        store = ", ".join(f"{inf} {', '.join(versions)}" for inf, versions in packages.items())
    except OSError as exc:
        packages = {}
        store = str(exc)
    evidence = [f"DriverStore：{store or '未找到 ' + ', '.join(detection['infs'])}"]
    evidence.extend(f"{r.get('DeviceID')} → {r.get('InfName')} {r.get('DriverVersion')}" for r in bound)
    if not bound:
        evidence.append(windows.get("drivers_error") or "未取得匹配的硬件 ID 与已绑定驱动版本")
    if bound and all(r.get("IsSigned") and r.get("DriverVersion") == component["version"] for r in bound):
        return "ok", "；".join(evidence)
    if not bound and packages:
        # FTDI needs both bus and port INFs. Prolific's alternative INF names vary by chipset.
        required = detection.get("required_infs", [])
        present = {inf for inf, versions in packages.items() if component["version"] in versions}
        if present and set(required).issubset(present):
            return "ok", "；".join(evidence)
    if bound or packages or store or "drivers" not in windows:
        return "unknown", "；".join(evidence)
    return "missing", "；".join(evidence)


def inspect_product(component: dict, windows: dict) -> tuple[str, str]:
    detection = component["detection"]
    products = [
        r
        for r in windows.get("products") or []
        if re.search(detection["name_pattern"], r.get("DisplayName") or "")
    ]
    dll = detection.get("system_dll")
    path = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / (dll or "")
    evidence = [f"{r['DisplayName']} {r.get('DisplayVersion') or '版本未知'}" for r in products]
    if dll:
        evidence.append(str(path) + (" 已找到" if path.is_file() else " 缺失"))
    if component["id"] == "rs-visa":
        from pyvisa.ctwrapper.highlevel import IVIVisaLibrary

        candidates = [str(candidate) for candidate in IVIVisaLibrary.get_library_paths()]
        if candidates:
            evidence.append("PyVISA 发现的后端：" + "、".join(candidates))
            if not products:
                return "unknown", "；".join([*evidence, "后端提供商及版本待核实"])
    if not products:
        evidence.append(windows.get("products_error") or "未取得匹配的已安装产品名称和版本")
    if component["id"] in {"ni-4882", "keysight-iolibs"}:
        gpib = [r for r in windows.get("drivers") or [] if "GPIB" in str(r.get("DeviceName")).upper()]
        evidence.extend(f"{r.get('DeviceName')} {r.get('DeviceID')} {r.get('DriverVersion')}" for r in gpib)
        if not gpib:
            evidence.append("控制器绑定情况待核实")
    match = re.match(r"[\d.]+", component["version"] or "")
    if not match:
        return "unknown", "；".join([*evidence, "台账缺少可比较的版本"])
    numeric = match[0]

    def normalized(value):
        parts = value.split(".")
        if not all(part.isdigit() for part in parts):
            return None
        return tuple(map(int, parts)) + (0,) * max(0, 4 - len(parts))

    compatible = any(normalized(r.get("DisplayVersion") or "") == normalized(numeric) for r in products)
    if compatible and (not dll or path.is_file()):
        return "ok", "；".join(evidence)
    state = "unknown" if products or (dll and path.is_file()) or "products" not in windows else "missing"
    return state, "；".join(evidence)


def inspect_dhcp(component: dict, windows: dict) -> tuple[str, str]:
    rows = [
        r
        for r in windows.get("services") or []
        if "tftpd" in (str(r.get("Name")) + str(r.get("Binary"))).lower()
    ]
    if not rows:
        return (
            "missing" if "services" in windows else "unknown",
            windows.get("services_error") or "未取得 Tftpd64 服务注册、程序路径与版本",
        )
    evidence = "；".join(
        f"{r['Name']} {r.get('PathName')}，版本 {r.get('Version') or '未取得'}" for r in rows
    )
    for r in rows:
        if (
            Path(r.get("Binary") or "").name.lower()
            in {"tftpd64.exe", "tftpd64_svc.exe", "tftpd64-service.exe"}
            and (r.get("Version") or "").split(".")[:2] == component["version"].split(".")[:2]
        ):
            return "ok", evidence + "；实验网租约需现场验证"
    return "unknown", evidence
