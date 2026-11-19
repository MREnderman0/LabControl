"""Read-only discovery and optional session verification for a commissioned Moku."""

import argparse
import platform
from dataclasses import asdict
from datetime import datetime
from hashlib import sha256
from pathlib import Path

from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.instruments import DeviceCommand, DeviceError
from lab_control.instruments.common.hardware_paths import verify_runtime_paths
from lab_control.storage.hardware_documents import refresh_after_batch
from lab_control.storage.hardware_records import atomic_json
from lab_control.storage.runtime import hardware_lock_home, runtime_home

from ..discovery import Discovery
from ..protocol import ADAPTER, FOUR_SLOT_SESSION, MODEL, Rest, serial_id
from .records import publish_report


def preflight(resource, serial, *, workspace=False, verify_four_slot=False):
    serial = serial_id(serial)
    home, locks = runtime_home(), hardware_lock_home()
    verify_runtime_paths(home, locks)
    start = datetime.now().astimezone()
    folder = (
        home
        / "hardware-tests"
        / f"Moku-Pro-{serial.zfill(6)}"
        / (start.strftime("%Y%m%d-%H%M%S-%f") + "-preflight")
    )
    folder.mkdir(parents=True, exist_ok=False)
    scope = "信号源 ch1 → 物理连线 → 示波器 ch1 和频谱仪 ch1；仅执行只读安全预检"
    state = "未申请控制权、未修改设置。四个输出状态、50 Ω 负载及增益未核实；不能确认可恢复，不发信号。"
    if verify_four_slot:
        scope = "核对指定资产身份，建立并释放四槽会话，检查槽位分配。"
        state = "四槽会话核验不启动采集或输出。"
    stages = {
        k: {"state": "not_tested", "reason": state}
        for k in (
            "enumeration",
            "identity",
            "compatibility_connection",
            "parameter_read",
            "configuration_backup",
            "parameter_write",
            "dual_record",
            "single_stop",
            "replay_export",
            "restoration",
            "reconnection",
            "physical_output",
        )
    }
    report = dict(
        adapter_id=ADAPTER,
        target=MODEL,
        expected_serial=serial,
        resource=resource,
        scope=scope,
        started_at=start.isoformat(),
        environment={"os": platform.platform(), "python": platform.python_version()},
        source_sha256=sha256(Path(__file__).read_bytes()).hexdigest(),
        stages=stages,
        events=[],
        initial_state=state,
        final_state=state,
    )
    stage = "process_lock"
    try:
        with ApplicationRuntime(root=home, hardware=True) as runtime:
            # This explicit acceptance run must not dispatch startup/auto-retry work.
            runtime.discovery.on_complete = None
            runtime.record_on_connect = {}
            candidates = []
            stages[stage] = {"state": "passed", "reason": "取得项目实机进程锁"}
            try:
                candidates = [r for r in Discovery().enumerate() if r.config.expected_serial == serial]
                report["discovered"] = [asdict(r) for r in candidates]
                stages["enumeration"] = {
                    "state": "passed" if candidates else "failed",
                    "reason": f"型号发现提供器：匹配候选 {len(candidates)}",
                }
            except Exception as exc:
                stages["enumeration"] = {"state": "failed", "reason": str(exc)}
            stage = "identity"
            selected = [r for r in candidates if r.config.resource == resource]
            if len(selected) != 1:
                raise DeviceError("当前枚举与指定地址不唯一匹配，不登记或连接")

            api = Rest(resource, 4, report["events"])
            api_identity = api.identity(serial)
            report["identity"] = asdict(api_identity)
            stages[stage] = {"state": "passed", "reason": "REST 完整型号、4.4 版本及指定序列号一致"}
            stage = "compatibility_connection"

            class Selected:
                name = Discovery.name

                def enumerate(self):
                    return tuple(selected)

            scan = runtime.discovery.scan(
                (Selected(),), preserve_presence=True, scope="single_moku_asset"
            ).result()
            report["workspace_scan"] = scan
            items = scan["items"]
            if len(items) != 1 or items[0]["reason"] != "identified":
                raise DeviceError("只读项目扫描未核验单一资产")
            device_id = items[0]["device_id"]
            first = runtime.connect(device_id, configure_acquisition=False).result()
            if first != api_identity:
                raise DeviceError("会话身份与只读预检不一致")
            stages[stage] = {
                "state": "passed",
                "reason": "已登记单一资产、取得项目会话锁并只读连接；未申请厂商控制权",
            }
            stage = "reconnection"
            runtime.sessions.disconnect(device_id)
            second = runtime.connect(device_id, configure_acquisition=False).result()
            if second != first:
                raise DeviceError("重连后身份不一致")
            stages[stage] = {"state": "passed", "reason": "断开后重新建立只读身份会话；不代表三槽控制重连"}
            stage = "parameter_read"
            report["device_summary"] = api.call("GET", "moku/summary")
            stages[stage] = {"state": "passed", "reason": "已取得整机摘要；这不是输出开关或可恢复备份。"}
            stages["configuration_backup"] = {
                "state": "blocked",
                "reason": "未证明当前模式和四路输出可回滚，禁止为备份先切换模式。",
            }
            stage = "existing_workspace"
            stages[stage] = {
                "state": "not_tested",
                "reason": "只读发现不建立厂商会话。已部署的资产可用 --verify-four-slot 核对槽位。",
            }
            if verify_four_slot:
                row = runtime.inventory.by_resource(resource)
                if not row or FOUR_SLOT_SESSION not in row["config"].get("options", ()):
                    raise DeviceError("此资产尚未登记四槽模式")
                result = runtime.sessions.submit(
                    device_id, "apply", DeviceCommand("attach_workspace", confirmed=True)
                ).result(80)
                if result.outcome != "success":
                    raise DeviceError(result.error)
                report["assigned_measurement_slots"] = result.readback["slots"]
                stages[stage] = {
                    "state": "passed",
                    "reason": "已通过正式适配器读取四槽名单，核对三个角色的槽号。",
                }
            if workspace:
                stage = "readonly_workspace"
                from .readonly_workspace import inspect_workspace

                report["workspace_panels"] = inspect_workspace(
                    runtime, device_id, folder, attached=verify_four_slot
                )
                stages[stage] = {
                    "state": "passed",
                    "reason": "正式分类卡片分别打开三个 ch1 面板，共用一个在线会话。",
                }
            stage = "session_close"
        stages[stage] = {"state": "passed", "reason": "运行时会话与进程锁已释放"}
    except Exception as exc:
        stages[stage] = {"state": "blocked", "reason": f"{type(exc).__name__}: {exc}"}
    finally:
        report["finished_at"] = datetime.now().astimezone().isoformat()
        atomic_json(folder / "results.json", report)
        lines = [f"# {MODEL} 只读预检", "", scope, "", state, "", "| 项目 | 结果 | 说明 |", "|---|---|---|"]
        lines.extend(f"| {key} | {row['state']} | {row['reason']} |" for key, row in stages.items())
        (folder / "record.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        publish_report(folder)
        refresh_after_batch(folder)
    return folder


if __name__ == "__main__":
    args = argparse.ArgumentParser(description=__doc__)
    args.add_argument("--resource", required=True)
    args.add_argument("--serial", required=True)
    args.add_argument(
        "--verify-four-slot", action="store_true", help="取得非强制所有权，选择已登记的四槽模式并读取槽位"
    )
    args.add_argument(
        "--workspace", action="store_true", help="通过正式工作区的三个类别打开指定资产，只读检查"
    )
    ns = args.parse_args()
    print(preflight(ns.resource, ns.serial, workspace=ns.workspace, verify_four_slot=ns.verify_four_slot))
