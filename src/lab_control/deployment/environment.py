"""Environment reports and proposed repairs; no repair executor is exposed."""

from __future__ import annotations

import copy
import importlib.metadata
import json
import os
import platform
import shutil
import struct
import sys
import tempfile
import threading
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from lab_control.application.config import ConnectionConfig
from lab_control.deployment.catalog import (
    cache_directories,
    load_manifest,
    payload_status,
    required_components,
)

STATES = {"ok", "missing", "incompatible", "unknown", "not_required", "error"}


def _version_tuple(value: Any) -> tuple[int, ...] | None:
    """Parse Windows package versions without adding a runtime dependency."""
    if value is None:
        return None
    text = str(value).strip().lstrip("vV")
    parts = text.split(".")
    if not parts or any(not part.isdigit() for part in parts):
        return None
    numbers = [int(part) for part in parts]
    return tuple(numbers + [0] * max(0, 4 - len(numbers)))


def export_report(report: dict, path: Path) -> None:
    """Atomically export an explicitly chosen report. No repair is performed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".environment-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def plan_repair(report: dict) -> dict:
    actions = []
    for item in report.get("items", []):
        if item["state"] in {"ok", "not_required"} or not item.get("required", True):
            continue
        actions.append(
            {
                "id": item["id"],
                "title": item["title"],
                "reason": item["reason"],
                "action": item["action"],
                "affected": item["affected"],
                "payload": item.get("payload", {"state": "not_applicable"}),
                "requires_admin": item.get("requires_admin", False),
                "reboot": item.get("reboot", "no"),
                "state": "needs_verification" if item["state"] in {"unknown", "error"} else "proposed",
            }
        )
    return {
        "schema_version": 1,
        "kind": "repair_plan",
        "executed": False,
        "executor_available": False,
        "inspection_complete": report.get("state") == "complete",
        "notice": "自动修复待安装维护组件接入",
        "actions": actions,
    }


def _storage(root: Path) -> tuple[str, str]:
    # A temporary write is the only mutation performed by environment inspection.
    if not root.is_dir():
        return "missing", "数据目录不存在；请检查目录设置"
    free = shutil.disk_usage(root).free
    with tempfile.TemporaryFile(dir=root) as stream:
        stream.write(b"LabControl storage check")
        stream.flush()
        os.fsync(stream.fileno())
    if free < 1024**3:
        return "unknown", f"数据目录可写；剩余 {free / 1024**3:.2f} GiB，请确认采集所需容量"
    return "ok", f"数据目录可写；剩余 {free / 1024**3:.1f} GiB"


def expected_versions() -> dict[str, str]:
    bundled = Path(__file__).with_name("release-manifest.json")
    lock = Path(__file__).resolve().parents[3] / "uv.lock"
    if bundled.is_file():
        packages = json.loads(bundled.read_text(encoding="utf-8"))["packages"]
    elif lock.is_file():
        packages = tomllib.loads(lock.read_text(encoding="utf-8"))["package"]
    else:
        return {}
    return {item["name"].lower(): item["version"] for item in packages}


def _inspect_sdk_routes(component: dict, configs) -> tuple[str, str]:
    from lab_control.application.environment import SDK_LIBRARIES, dependency_diagnostics

    routes = {c.transport for c in configs if c.transport in component["transports"]}
    kinds = tuple(sorted(routes or {component["sdk"]}))
    checks = dependency_diagnostics(configs, sdk_kinds=kinds)["sdk"]
    states, evidence = [], []
    for kind in kinds:
        detail = checks[kind]
        libs = detail["libraries"]
        explicit = {c.sdk_path for c in configs if c.transport == kind and c.sdk_path}
        automatic = any(c.transport == kind and not c.sdk_path for c in configs) or not routes
        if automatic:
            default = next((entry for entry in libs if entry.get("bits") == 64), None)
            if default:
                explicit.add(default["path"])
        selected = [entry for entry in libs if entry["path"] in explicit]
        state = detail["state"]
        if any(entry["state"] != "loadable" for entry in selected):
            state = next(entry["state"] for entry in selected if entry["state"] != "loadable")
        states.append(state)
        evidence.append(
            f"{kind} ({SDK_LIBRARIES[kind][0]})："
            + (
                "；".join(entry["path"] + " " + (entry.get("error") or entry["state"]) for entry in libs)
                or "缺少 x64 DLL"
            )
        )
    state = "incompatible" if "error" in states else "missing" if "missing" in states else "ok"
    return state, "；".join(evidence)


def inspect_environment(
    root: Path,
    configs: tuple[ConnectionConfig, ...] = (),
    *,
    lan: bool = False,
    dhcp: bool = False,
    cancel: threading.Event | None = None,
    progress: Callable[[dict], None] | None = None,
    caches: tuple[Path, ...] | None = None,
    selected_components: tuple[str, ...] = (),
) -> dict:
    cancel = cancel or threading.Event()
    report: dict[str, Any] = {
        "schema_version": 1,
        "kind": "environment_inspection",
        "state": "running",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "platform": platform.platform(),
        "root": str(root),
        "hardware_accessed": False,
        "settings_changed": False,
        "executor_available": False,
        "items": [],
        "observations": {},
    }

    def add(key, title, state, reason, action, affected=(), **extra):
        assert state in STATES
        report["items"].append(
            {
                "id": key,
                "title": title,
                "state": state,
                "reason": reason,
                "action": action,
                "affected": list(affected),
                **extra,
            }
        )
        if progress:
            progress(copy.deepcopy(report))

    def finish():
        report["state"] = "cancelled" if cancel.is_set() else "complete"
        report["repair_plan"] = plan_repair(report)
        return report

    supported = (
        sys.platform == "win32"
        and struct.calcsize("P") == 8
        and platform.machine().lower() in {"amd64", "x86_64"}
    )
    if sys.platform == "win32":
        supported = (
            supported and sys.getwindowsversion().build >= 17763 and sys.getwindowsversion().product_type == 1
        )
    add(
        "platform",
        "操作系统与架构",
        "ok" if supported else "incompatible",
        platform.platform(),
        "使用已验证的 Windows 10/11 x64 安装包",
    )
    try:
        manifest = load_manifest()
        needed = required_components(configs, lan=lan, dhcp=dhcp)
        from lab_control.deployment.materials import component_closure

        needed.update(c["id"] for c in component_closure(selected_components, manifest))
        add("manifest", "组件台账", "ok", "组件清单已加载", "使用同版本离线安装包修复程序文件")
    except Exception as exc:
        add("manifest", "组件台账", "error", str(exc), "使用同版本离线安装包修复程序文件")
        return finish()
    try:
        expected = expected_versions()
    except (OSError, ValueError, KeyError) as exc:
        expected = {}
        add("versions", "发行版本清单", "error", str(exc), "使用同版本安装包修复版本清单")
    for name, module in {**manifest["python_roots"], **manifest.get("python_transitive_roots", {})}.items():
        if cancel.is_set():
            return finish()
        try:
            version = importlib.metadata.version(name)
            importlib.import_module(module)
            wanted = expected.get(name.lower())
            state = "ok" if version == wanted else "incompatible" if wanted else "unknown"
            add(
                "python:" + name,
                name,
                state,
                f"检测版本 {version}；发行要求 {wanted or '待核实'}",
                "使用同版本离线安装包修复应用运行库",
            )
        except importlib.metadata.PackageNotFoundError as exc:
            add(
                "python:" + name,
                name,
                "missing",
                f"运行包或版本元数据缺失：{exc}",
                "使用同版本离线安装包修复应用运行库",
            )
        except Exception as exc:
            add(
                "python:" + name,
                name,
                "error",
                f"导入 {module} 失败：{exc}",
                "使用同版本离线安装包修复应用运行库",
            )
    try:
        from PySide6.QtGui import QImageReader

        from lab_control.app import ICON_PATH, load_stylesheet
        from lab_control.instruments.devices.catalog import load_definitions

        if QImageReader(str(ICON_PATH)).read().isNull():
            raise ValueError("无法解码应用图标：" + str(ICON_PATH))
        if not load_definitions():
            raise ValueError("无法读取仪器型号注册")

        if not load_stylesheet().strip():
            raise ValueError("主题文件为空")
        add("resources", "应用资源", "ok", "主题、图标和型号注册可读取", "使用同版本离线安装包修复应用文件")
    except Exception as exc:
        add("resources", "应用资源", "error", str(exc), "使用同版本离线安装包修复应用文件")
    try:
        state, reason = _storage(root)
        add("storage", "数据目录", state, reason, "检查保存路径、可用容量和当前用户权限")
    except OSError as exc:
        add("storage", "数据目录", "error", str(exc), "检查保存路径、可用容量和当前用户权限")
    if cancel.is_set():
        return finish()
    windows = {}
    try:
        from lab_control.deployment.windows import inspect_windows

        windows = inspect_windows(cancel)
        report["observations"]["windows"] = windows
    except InterruptedError:
        return finish()
    except Exception as exc:
        add("windows", "Windows 组件检查", "unknown", str(exc), "重试检查并导出报告", requires_admin=False)
    if cancel.is_set():
        return finish()
    if lan or dhcp:
        try:
            from lab_control.application.network_setup import inspect_network

            network = inspect_network(cancel)
            report["observations"]["network"] = network
            usable = [a for a in network["adapters"] if a["up"] and a["ethernet"] and a["addresses"]]
            add(
                "network",
                "实验网络",
                "ok" if usable else "missing",
                f"发现 {len(usable)} 个具有 IPv4 地址的已连接物理有线网口；实验接口需核对",
                "在部署向导中选择实验网口并重新生成本机网络方案",
                requires_admin=True,
            )
            add(
                "dhcp-state",
                "DHCP 状态",
                "unknown",
                f"本机 UDP 67 监听者 {len(network.get('dhcp_listeners', []))} 个；不能据此判断网络中其他 DHCP",
                "核对实验网口与已有 DHCP；独立实验网按本机方案部署",
                requires_admin=True,
            )
        except Exception as exc:
            add("network", "实验网络", "unknown", str(exc), "重试检查；核对当前实验网卡", requires_admin=True)
        add(
            "firewall",
            "防火墙",
            "unknown",
            windows.get("firewall_error")
            or "已记录可读取的配置；未进行报文测试，尚未核实部署规则与实际程序路径",
            "由安装维护组件按实验网口和当前程序路径核验并恢复应用规则",
            requires_admin=True,
        )
    else:
        add(
            "network",
            "实验网络 / DHCP / 防火墙",
            "not_required",
            "当前连接未启用实验网络",
            "按实际设备路线启用",
        )
    from lab_control.deployment.file_components import inspect_files_usb
    from lab_control.deployment.windows import inspect_dhcp, inspect_product, inspect_serial
    from lab_control.instruments.devices.catalog import load_definitions

    definitions = load_definitions()
    entries = manifest["components"]
    groups = {c["id"]: c["members"] for c in entries if c.get("members")}
    folders = caches if caches is not None else cache_directories()
    registered = {c.adapter for c in configs if c.transport != "mock"}
    # Evaluate concrete drivers first so interface rows can report each alternative.
    items: dict[str, dict] = {}
    for component in sorted(entries, key=lambda c: bool(c.get("members"))):
        if cancel.is_set():
            return finish()
        key = component["id"]
        kind = component["detection"]["kind"]
        aliases = {group for group, members in groups.items() if key in members}
        applicable = {
            d.adapter_id
            for d in definitions.values()
            if aliases.union({key}).intersection(d.deployment_components)
        }
        applicable.update(component["adapters"])
        if kind == "model_runtime":
            applicable.add(component["detection"]["adapter"])
        affected = sorted(applicable)
        state, reason = "unknown", "缺少可核验的安装信息"
        if key == "msvc-runtime":
            vc = windows.get("vc_runtime") or {}
            if vc.get("Installed") == 1:
                actual = _version_tuple(vc.get("Version"))
                minimum = _version_tuple(
                    component.get("version") or component["detection"].get("minimum_version")
                )
                if actual and minimum:
                    state = "ok" if actual >= minimum else "incompatible"
                    comparator = "不低于" if state == "ok" else "低于"
                    reason = f"检测到 VC++ {vc.get('Version')}；要求版本 {component.get('version')}，已安装版本{comparator}要求"
                else:
                    state, reason = "unknown", f"检测到 VC++ {vc.get('Version')}；版本格式或目标约束待核实"
            elif vc:
                state, reason = "missing", vc.get("vc_runtime_error", "未检测到已安装的 VC++ x64 运行库")
            else:
                state, reason = "unknown", windows.get("vc_runtime_error", "无法核实 VC++ 运行库")
        else:
            try:
                if kind == "interface_group":
                    members = [items[k] for k in groups[key]]
                    evidence = {
                        "ok": "已检测",
                        "missing": "缺失",
                        "unknown": "待核实",
                        "incompatible": "不兼容",
                        "error": "检查失败",
                    }
                    reason = "；".join(
                        f"{m['title']}：{evidence.get(m['state'], m['state'])}" for m in members
                    )
                    states = {m["state"] for m in members}
                    state = "ok" if "ok" in states else "missing" if states == {"missing"} else "unknown"
                elif kind == "model_runtime":
                    model = definitions.get(component["detection"]["adapter"])
                    if model and model.inspect_component:
                        state, reason = model.inspect_component(component)
                    else:
                        reason = "缺少已登记型号的运行库检测入口"
                elif kind == "files_usb":
                    state, reason = inspect_files_usb(
                        component["detection"], tuple(c for c in configs if c.adapter in applicable)
                    )
                elif kind == "serial_driver":
                    state, reason = inspect_serial(component, windows)
                elif kind == "windows_product":
                    state, reason = inspect_product(component, windows)
                elif kind == "dhcp_service":
                    state, reason = inspect_dhcp(component, windows)
                elif kind == "sdk":
                    state, reason = _inspect_sdk_routes(component, configs)
            except Exception as exc:
                state, reason = "error", str(exc)
        payload = {"state": "not_applicable"} if key in groups else payload_status(component, folders, cancel)
        if cancel.is_set():
            return finish()
        action = component.get("manual_steps") or "在组件准备页勾选实际使用的驱动，安装后重新检查"
        requires_admin = component["installation"]["requires_admin"]
        reboot = component["installation"]["reboot"]
        if key == "msvc-runtime" and state == "ok":
            action = "已安装不低于要求版本；跳过安装并禁止降级"
            requires_admin = False
            reboot = "no"
        add(
            key,
            component["name"],
            state,
            reason,
            action,
            affected,
            required=key in needed,
            scope=component["scope"],
            registered_models=sorted(applicable & registered),
            payload=payload,
            requires_admin=requires_admin,
            reboot=reboot,
        )
        items[key] = report["items"][-1]
    return finish()
