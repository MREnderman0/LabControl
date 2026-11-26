import asyncio
import json
import socket
import sys
import threading
import time

import httpx2
import pytest

from lab_control.agent.connector import remote_client
from lab_control.agent.server import AgentServer
from lab_control.application.agents.policy import INTERNAL_CLIENT
from lab_control.application.config import ConnectionConfig
from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.instruments import DeviceError


def wait_task(service, client, grant, task, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = service.get_task_status(client, grant, task["id"])
        if value["state"] not in {"accepted", "running", "stopping", "converting", "binary_saved"}:
            return value
        time.sleep(0.01)
    raise AssertionError(value)


@pytest.fixture
def experiment(tmp_path):
    config = ConnectionConfig("mock://agent/scope", "rs.rtm3004", "mock")
    with ApplicationRuntime((config,), root=tmp_path, hardware=False) as runtime:
        device = runtime.add(config)
        runtime.connect(device).result()
        service = runtime.agent
        client = INTERNAL_CLIENT
        grant = service.policy.create_grant(
            client,
            [device],
            [
                "view",
                "connect",
                "disconnect",
                "read_parameters",
                "apply",
                "acquire",
                "monitor",
                "record",
                "stop",
                "results",
            ],
            channels=[1],
            operations=["timebase"],
            limits={"timebase": {"value": [0.0001, 0.01]}},
        )
        yield runtime, service, client, grant, device


def test_agent_changes_share_session_deduplicate_and_reject_stale_preview(experiment):
    runtime, service, client, grant, device = experiment
    session = runtime.sessions.sessions[device]
    preview = service.preview_change(client, grant, device, "timebase", {"value": 0.002})
    task = service.apply_change(client, grant, device, preview["id"], "write-1")
    result = wait_task(service, client, grant, task)
    assert result["state"] == "completed", result
    version = session.configuration_version
    assert service.apply_change(client, grant, device, preview["id"], "write-1")["id"] == task["id"]
    assert session.configuration_version == version
    stale = service.apply_change(client, grant, device, preview["id"], "write-2")
    assert wait_task(service, client, grant, stale)["code"] == "configuration_conflict"
    assert session.state == "connected"
    with pytest.raises(DeviceError, match="范围"):
        service.preview_change(client, grant, device, "timebase", {"value": 0.02})


def test_guard_rechecks_grant_inside_device_worker(experiment):
    runtime, service, client, grant, device = experiment
    session = runtime.sessions.sessions[device]
    release = threading.Event()
    blocker = session.worker.submit(release.wait, 3)
    preview = service.preview_change(client, grant, device, "timebase", {"value": 0.002})
    task = service.apply_change(client, grant, device, preview["id"], "revoked-write")
    service.policy.revoke_grant(grant)
    release.set()
    blocker.result()
    deadline = time.monotonic() + 4
    while service.ledger.get(task["id"])["state"] in {"accepted", "running", "stopping"}:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert session.configuration_version == 0 and session.pending == 0
    assert service.ledger.get(task["id"])["code"] == "grant_expired"


def test_shared_monitor_recording_stop_and_artifacts(experiment):
    runtime, service, client, grant, device = experiment
    task = service.start_recording(
        client, grant, device, {"kind": "waveform", "points": 32}, "record-1", 0.05
    )
    deadline = time.monotonic() + 5
    while not runtime.monitoring.latest(device, "waveform"):
        assert time.monotonic() < deadline
        time.sleep(0.01)
    conflict = service.start_monitoring(client, grant, device, {"kind": "waveform"}, "conflict")
    assert wait_task(service, client, grant, conflict)["code"] == "task_conflict"
    latest = service.get_latest_measurement(client, grant, device, "waveform")
    assert latest["available"] and latest["measurements"][0]["points"] == 32
    stop = service.stop_task(client, grant, task["id"], "stop-1")
    assert wait_task(service, client, grant, stop)["state"] == "completed"
    assert wait_task(service, client, grant, task)["state"] == "completed"
    artifacts = service.list_artifacts(client, grant, task["id"])
    assert any(a["name"].endswith(".csv") for a in artifacts)
    assert all("path" not in a for a in artifacts)
    assert not runtime.sessions.sessions[device].controls_locked


def test_operator_stop_during_recording_setup_reports_cancelled(experiment, monkeypatch):
    runtime, service, client, grant, device = experiment
    entered, release = threading.Event(), threading.Event()
    create = runtime.repository.create_recording

    def wait_for_operator(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return create(*args, **kwargs)

    monkeypatch.setattr(runtime.repository, "create_recording", wait_for_operator)
    task = service.start_recording(client, grant, device, {"kind": "waveform"}, "cancel-during-setup")
    try:
        assert entered.wait(5)
        service.stop_local(task["id"])
    finally:
        release.set()
    result = wait_task(service, client, grant, task)
    assert result["state"] == "cancelled" and "未开始" in result["error"]
    assert not service.list_artifacts(client, grant, task["id"])


def test_sdk_http_without_token_enforces_origin_and_grants(experiment):
    runtime, service, client, grant, device = experiment
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    service.server = AgentServer(service)
    service.server.configure({"enabled": True, "local_port": port})
    url = f"http://127.0.0.1:{port}/mcp"
    with httpx2.Client(trust_env=False) as http:
        assert http.post(url, json={}, headers={"Origin": "http://evil.test"}).status_code == 403

    async def run():
        async with remote_client({"url": url}, True) as remote:
            listed = await remote.list_tools()
            assert len(listed.tools) == 27 and "save_frame" in {tool.name for tool in listed.tools}
            runtime.bind_slot(device, "oscilloscope:1", "general")
            slots = await remote.call_tool("list_slots", {"grant_id": grant})
            assert not slots.is_error
            slot = slots.structured_content["data"][0]
            assert slot["device_id"] == device and slot["slot_id"] == "oscilloscope:1"
            resolved = await remote.call_tool("resolve_slot", {"grant_id": grant, "slot_id": slot["slot_id"]})
            assert not resolved.is_error and resolved.structured_content["data"] == slot
            bad = await remote.call_tool("list_slots", {"grant_id": grant, "workspace": "invalid"})
            assert bad.is_error
            info = await remote.call_tool("get_server_info", {})
            assert not info.is_error, info
            assert [item["id"] for item in info.structured_content["data"]["grants"]] == [grant]
            assert info.structured_content["data"]["grants"][0]["expires_at"] is None
            denied = await remote.call_tool("list_devices", {"grant_id": "unknown"})
            assert denied.is_error and denied.structured_content["code"] == "grant_expired"
            params = {"grant_id": grant, "device_id": device}
            result = await remote.call_tool(
                "preview_change", {**params, "operation": "timebase", "parameters": {"value": 0.003}}
            )
            assert not result.is_error, result
            data = json.loads(result.content[0].text)["data"]
            result = await remote.call_tool(
                "apply_change", {**params, "preview_id": data["id"], "request_id": "sdk-1"}
            )
            assert not result.is_error, result
            task = json.loads(result.content[0].text)["data"]
            done = await asyncio.to_thread(wait_task, service, client, grant, task)
            assert done["state"] == "completed", done
            rejected = await remote.call_tool(
                "apply_change", {**params, "preview_id": data["id"], "request_id": "sdk-2", "confirmed": True}
            )
            assert rejected.is_error
            monitor = await remote.call_tool(
                "start_monitoring",
                {**params, "request": {"kind": "waveform", "points": 16}, "request_id": "disconnect-monitor"},
            )
            assert not monitor.is_error
            monitor_id = monitor.structured_content["data"]["id"]
        # Transport disconnect does not own the lifetime of accepted work.
        await asyncio.sleep(0.15)
        async with remote_client({"url": url}, True) as reconnected:
            result = await reconnected.call_tool(
                "get_task_status", {"grant_id": grant, "task_id": monitor_id}
            )
            assert result.structured_content["data"]["state"] == "running"
            stopped = await reconnected.call_tool(
                "stop_task", {"grant_id": grant, "task_id": monitor_id, "request_id": "disconnect-stop"}
            )
            assert not stopped.is_error
            await asyncio.to_thread(wait_task, service, client, grant, stopped.structured_content["data"])

    asyncio.run(run())


@pytest.fixture
def single_experiment(experiment):
    runtime, service, client, _grant, device = experiment
    grant = service.policy.create_grant(
        client,
        [device],
        ["view", "acquire", "output_trigger", "results", "stop"],
        channels=[1],
        operations=[],
        limits={},
    )
    return runtime, service, client, grant, device


def test_request_conflict_channels_and_result_authority(single_experiment):
    _runtime, service, client, grant, device = single_experiment
    task = service.acquire_once(client, grant, device, {"kind": "waveform", "points": 16}, "once")
    assert wait_task(service, client, grant, task)["state"] == "completed"
    with pytest.raises(DeviceError, match="request_id"):
        service.acquire_once(client, grant, device, {"kind": "waveform", "points": 32}, "once")
    with pytest.raises(DeviceError, match="通道"):
        service.acquire_once(client, grant, device, {"channels": [2]}, "other-channel")
    other_grant = service.policy.create_grant(
        client, [device], ["view", "results", "stop"], channels=[2], operations=[], limits={}
    )
    artifact = service.list_artifacts(client, grant, task["id"])[0]
    assert not service.list_artifacts(client, other_grant, task["id"])
    assert not service.list_tasks(client, other_grant)
    with pytest.raises(DeviceError, match="通道"):
        service.get_artifact(client, other_grant, artifact["id"])
    with pytest.raises(DeviceError, match="通道"):
        service.stop_task(client, other_grant, task["id"], "stop-other-channel")
    with pytest.raises(DeviceError):
        service.artifact_path(client, grant, "../../secret")


def test_revocation_and_stop_with_separate_grant(experiment):
    runtime, service, client, grant, device = experiment
    other_grant = service.policy.create_grant(
        client, [device], ["view", "stop"], channels=[1], operations=[], limits={}
    )
    task = service.start_monitoring(client, grant, device, {"kind": "waveform", "points": 16}, "monitor")
    assert any(t["id"] == task["id"] for t in service.list_tasks(client, other_grant))
    stop = service.stop_task(client, other_grant, task["id"], "shared-stop")
    assert wait_task(service, client, other_grant, stop)["state"] == "completed"
    task = service.start_recording(client, grant, device, {"kind": "waveform", "points": 16}, "revoke")
    # A legacy timestamp does not expire a grant while the application is running.
    service.policy.grants[grant]["expires_at"] = 1
    assert service.get_task_status(client, grant, task["id"])["state"] == "running"
    service.policy.revoke_grant(grant)
    service.wake.set()
    assert wait_task(service, client, other_grant, task)["state"] in {"completed", "failed"}
    assert not runtime.sessions.sessions[device].controls_locked
    assert not runtime.monitoring.tasks[device].running


def test_audit_failure_blocks_writes_but_allows_stop(experiment, monkeypatch):
    runtime, service, client, grant, device = experiment
    task = service.start_monitoring(client, grant, device, {"kind": "waveform"}, "monitor-audit")
    preview = service.preview_change(client, grant, device, "timebase", {"value": 0.002})

    def failed(*args, **kwargs):
        raise OSError("audit unavailable")

    monkeypatch.setattr(service.repository, "audit", failed)
    with pytest.raises(OSError, match="audit unavailable"):
        service.apply_change(client, grant, device, preview["id"], "rejected-audit")
    stop = service.stop_task(client, grant, task["id"], "audit-stop")
    assert wait_task(service, client, grant, stop)["state"] == "completed"
    assert runtime.sessions.sessions[device].configuration_version == 0


def test_gui_task_explicit_stop_and_monitor_guard(experiment):
    from lab_control.domain.instruments import AcquisitionRequest

    runtime, service, client, grant, device = experiment
    runtime.monitoring.start(device, (AcquisitionRequest(kind="waveform", points=16),))
    tasks = service.list_tasks(client, grant)
    gui = next(t for t in tasks if t["id"].startswith("gui-"))
    stop = service.stop_task(client, grant, gui["id"], "gui-stop")
    assert wait_task(service, client, grant, stop)["state"] == "completed"
    service.start_monitoring(client, grant, device, {"kind": "waveform", "points": 16}, "guard")
    monitor = runtime.monitoring.tasks[device]
    assert not monitor.requests[0].allow_setup
    service.policy.revoke_grant(grant)
    with pytest.raises(DeviceError, match="授权"):
        monitor.before_acquire(runtime.sessions.sessions[device], monitor.requests[0])


def test_read_only_connect_and_restart_never_replays(tmp_path):
    from lab_control.storage.agent_ledger import AgentLedger

    config = ConnectionConfig("mock://agent/restart", "rs.rtm3004", "mock")
    with ApplicationRuntime((config,), root=tmp_path, hardware=False) as runtime:
        device = runtime.add(config)
        runtime.connect(device).result()
        service = runtime.agent
        client = INTERNAL_CLIENT
        grant = service.policy.create_grant(
            client, [device], ["view", "connect", "disconnect"], channels=[1], operations=[], limits={}
        )
        runtime.sessions.disconnect(device)
        task = service.connect_device(client, grant, device, "read-only")
        assert wait_task(service, client, grant, task)["state"] == "completed"
        assert not runtime.sessions.sessions[device].acquisition_setup_enabled
        denied = service.connect_device(client, grant, device, "initialize", True)
        assert wait_task(service, client, grant, denied)["code"] == "permission_denied"
        unknown = {**task, "id": "lost-command", "request_id": "unknown", "state": "running"}
        service.ledger.insert(unknown, AgentLedger.fingerprint("unknown"))
    with ApplicationRuntime((config,), root=tmp_path, hardware=False) as runtime:
        service = runtime.agent
        assert not service.policy.grants[grant]["enabled"]
        with pytest.raises(DeviceError, match="授权"):
            service.list_slots(client, grant)
        assert service.ledger.get("lost-command")["state"] == "interrupted"
        assert not runtime.sessions.sessions


def test_stdio_connector_has_only_protocol_stdout(experiment, tmp_path):
    from mcp import Client
    from mcp.client.stdio import StdioServerParameters

    _runtime, service, _client, _grant, _device = experiment
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    service.server = AgentServer(service)
    service.server.configure({"enabled": True, "local_port": port})
    path = tmp_path / "connection 中文.json"
    service.server.export_connection(path)
    exported = json.loads(path.read_text(encoding="utf-8"))
    assert set(exported) == {"schema_version", "url"}
    path.write_text(json.dumps({**exported, "token_env": "LEGACY_UNUSED_TOKEN"}), encoding="utf-8")

    async def run():
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "lab_control", "--agent-connector", "--connection", str(path)],
        )
        async with Client(parameters, read_timeout_seconds=10) as client:
            listed = (await client.list_tools()).tools
            assert len(listed) == 28 and {"save_frame", "download_frame"} <= {tool.name for tool in listed}
            result = await client.call_tool("get_server_info", {})
            assert not result.is_error and result.structured_content["data"]["simulation"]

    asyncio.run(run())


def test_loopback_port_change_rollback_and_download(single_experiment, tmp_path):
    from lab_control.agent.connector import connection, download
    from lab_control.agent.server import saved_settings

    runtime, service, client, grant, device = single_experiment
    server = service.server = AgentServer(service)
    with socket.socket() as reserve:
        reserve.bind(("127.0.0.1", 0))
        port = reserve.getsockname()[1]
    server.configure({"enabled": True, "local_port": port})
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        other = occupied.getsockname()[1]
        with pytest.raises(OSError, match=str(other)):
            server.configure({"enabled": True, "local_port": other})
        assert saved_settings(runtime.repository)["local_port"] == port
        with httpx2.Client(trust_env=False) as http:
            assert http.post(server.url, headers={"Host": "invalid.test"}).status_code == 403
    server.configure({"enabled": True, "local_port": other})
    with socket.socket() as probe:
        assert probe.connect_ex(("127.0.0.1", port)) != 0
    path = tmp_path / "local.json"
    server.export_connection(path)
    config, verify = connection(path)
    assert config["url"] == f"http://127.0.0.1:{other}/mcp"
    with httpx2.Client(trust_env=False) as http:
        assert http.post(config["url"], content=b"x" * (1024 * 1024 + 1)).status_code == 413
    task = service.acquire_once(client, grant, device, {"kind": "waveform", "points": 16}, "port-change-data")
    assert wait_task(service, client, grant, task)["state"] == "completed"
    artifact = service.list_artifacts(client, grant, task["id"])[0]
    output = tmp_path / "downloaded"
    assert download(config, verify, artifact["id"], grant, output)["bytes"] == artifact["size"]
    download_path = service.get_artifact(client, grant, artifact["id"])["download_path"]
    service.policy.revoke_grant(grant)
    with httpx2.Client(verify=verify, trust_env=False) as http:
        assert http.get(config["url"].removesuffix("/mcp") + download_path).status_code == 403


@pytest.mark.parametrize("adapter", ["keithley.2231a", "rigol.dg5504pro", "rs.rtm3004", "keysight.3458a"])
def test_cross_model_capabilities_and_output_authority(tmp_path, adapter):
    config = ConnectionConfig("mock://agent/cross-model", adapter, "mock")
    with ApplicationRuntime((config,), root=tmp_path, hardware=False) as runtime:
        device = runtime.add(config)
        runtime.connect(device, configure_acquisition=False).result()
        service = runtime.agent
        client = INTERNAL_CLIENT
        capabilities = runtime.sessions.capability_snapshot(device).channels[1]
        operations = [c.name for c in capabilities.operations if c.writable]
        actions = ["view", "apply", "acquire"]
        if adapter == "rigol.dg5504pro":
            limited_grant = service.policy.create_grant(
                client, [device], actions, channels=[1], operations=operations, limits={}
            )
            denied = service.acquire_once(
                client, limited_grant, device, {"kind": "status", "quantity": "frequency"}, "status-read"
            )
            assert wait_task(service, client, limited_grant, denied)["code"] == "permission_denied"
            actions.append("read_parameters")
        if adapter in {"keysight.3458a", "rs.rtm3004"}:
            actions.append("output_trigger")
        grant = service.policy.create_grant(
            client, [device], actions, channels=[1], operations=operations, limits={}
        )
        metadata = service.get_capabilities(client, grant, device)
        assert metadata["snapshot"]["identity"]["model"]
        for capability in capabilities.operations:
            assert metadata["effects"]["1"][capability.name]["write"] == (
                "output_trigger" if capability.requires_confirmation else "configuration"
            )
        if "output" in operations:
            assert capabilities.get("output").requires_confirmation
            with pytest.raises(DeviceError, match="授权"):
                service.preview_change(client, grant, device, "output", {"value": True})
        request = service._request(
            client,
            grant,
            device,
            "acquire",
            {
                "kind": "waveform"
                if adapter == "rs.rtm3004"
                else "status"
                if adapter == "rigol.dg5504pro"
                else "scalar",
                "quantity": "frequency" if adapter == "rigol.dg5504pro" else "",
                "points": 8,
            },
        )
        assert not request.allow_setup
        task = service.acquire_once(
            client,
            grant,
            device,
            {"kind": request.kind, "quantity": request.quantity, "points": 8},
            "cross-model",
        )
        assert wait_task(service, client, grant, task)["state"] == "completed"


def test_panel_restores_local_settings_and_exports_matching_pair(experiment, qtbot, tmp_path, monkeypatch):
    import tomllib
    from pathlib import Path

    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QFileDialog

    from lab_control.ui.pages.agent_collaboration import AgentCollaborationPanel

    runtime, service, client, grant, device = experiment
    with socket.socket() as reserve:
        reserve.bind(("127.0.0.1", 0))
        port = reserve.getsockname()[1]
    runtime.repository.save_workspace(
        "agent/server",
        {
            "enabled": True,
            "local_port": port,
            "lan_enabled": True,
            "lan_address": "192.0.2.3",
            "lan_port": 8777,
            "local_commands": True,
        },
    )
    panel = AgentCollaborationPanel(runtime)
    qtbot.addWidget(panel)
    panel.show()
    try:
        qtbot.waitUntil(lambda: not panel.busy and panel.enabled.isChecked())
        assert panel.server.snapshot()["settings"] == {"enabled": True, "local_port": port}
        assert panel.server.listener.sock.getsockname() == ("127.0.0.1", port)
        assert runtime.repository.load_workspace("agent/server") == panel.server.settings
        assert panel.authority.text() == "已自动授权 1 台设备" and not panel.tasks.isVisible()
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            other = occupied.getsockname()[1]
            panel.local_port.setValue(other)
            panel.apply.click()
            qtbot.waitUntil(lambda: not panel.busy)
            assert str(other) in panel.status.text() and panel.address.text() == panel.server.url
            assert panel.server.url.endswith(f":{port}/mcp")
        qtbot.keyClick(panel.local_port, Qt.Key.Key_Return)
        qtbot.waitUntil(lambda: not panel.busy and panel.server.settings["local_port"] == other)
        panel.copy.click()
        assert QApplication.clipboard().text() == f"http://127.0.0.1:{other}/mcp"
        target = tmp_path / "配置.toml"
        monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a: (str(target), ""))
        panel.export.menu().actions()[1].trigger()
        cfg = tomllib.loads(target.read_text(encoding="utf-8"))["mcp_servers"]["lab_control"]
        assert json.loads(Path(cfg["args"][-1]).read_text(encoding="utf-8"))["url"] == panel.address.text()

        connection = Path(cfg["args"][-1])
        previous = connection.read_bytes(), target.read_bytes()
        panel.export.menu().actions()[1].trigger()
        assert (connection.read_bytes(), target.read_bytes()) == previous
        standalone = tmp_path / "连接.json"
        monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a: (str(standalone), ""))
        panel.export.menu().actions()[0].trigger()
        assert json.loads(standalone.read_text(encoding="utf-8"))["url"] == panel.server.url

        task = service.start_recording(
            client, grant, device, {"kind": "waveform", "points": 32}, "panel-record", interval=0.05
        )
        qtbot.waitUntil(
            lambda: any(
                item["id"] == task["id"] and item.get("recorded_frames", 0) > 0
                for item in service.snapshot(active_only=True)["tasks"]
            ),
            timeout=10000,
        )
        panel.refresh()
        assert task["id"] in panel.task_rows and panel.tasks.isVisible()
        panel.task_rows[task["id"]][2].click()
        qtbot.waitUntil(lambda: service.ledger.get(task["id"])["state"] == "completed", timeout=10000)
        panel.refresh()
        assert not panel.tasks.isVisible()
        assert any(a["name"].endswith(".csv") for a in service.list_artifacts(client, grant, task["id"]))
        panel.enabled.click()
        qtbot.waitUntil(lambda: not panel.busy and not panel.server.snapshot()["running"])
        assert not AgentServer(service).settings["enabled"]
    finally:
        panel.shutdown()


def test_automatic_grant_includes_all_verified_devices_without_io(experiment, qtbot, monkeypatch):

    from lab_control.application.agents.policy import ACTIONS
    from lab_control.ui.pages.agent_collaboration import AgentCollaborationPanel

    runtime, service, client, limited, scope = experiment
    source = runtime.add(ConnectionConfig("mock://agent/full-source", "rigol.dg5504pro", "mock"))
    runtime.connect(source).result()
    pending = runtime.add(ConnectionConfig("mock://agent/not-yet-identified", "rs.rtm3004", "mock"))
    panel = AgentCollaborationPanel(runtime)
    qtbot.addWidget(panel)

    def no_io(*args, **kwargs):
        pytest.fail("issuing an experiment grant must not access hardware")

    with monkeypatch.context() as guarded:
        guarded.setattr(runtime.sessions, "submit", no_io)
        guarded.setattr(runtime, "connect", no_io)
        guarded.setattr(runtime.discovery, "scan", no_io)
        panel.refresh()
    assert panel.authority.text() == "已自动授权 2 台设备"
    grant_id = service.policy.desktop_grant_id
    assert grant_id != limited
    grant = service.policy.grants[grant_id]
    assert grant["full_device_access"] and grant["expires_at"] is None
    assert set(grant["actions"]) == ACTIONS
    assert {d["device_id"] for d in service.list_devices(client, grant_id)} == {scope, source}

    preview = service.preview_change(client, grant_id, source, "output", {"value": True}, channel=2)
    assert preview["requires_output_authority"]
    task = service.apply_change(client, grant_id, source, preview["id"], "full-grant-output")
    assert wait_task(service, client, grant_id, task)["state"] == "completed"
    with pytest.raises(DeviceError):
        service.preview_change(client, grant_id, source, "output", {"value": "invalid"}, channel=2)
    with pytest.raises(DeviceError, match="通道"):
        service.read_parameters(client, limited, scope, ["coupling"], channel=2)

    runtime.connect(pending).result()
    panel.refresh()
    assert service.policy.desktop_grant_id == grant_id
    assert pending in service.policy.grants[grant_id]["devices"]
    assert panel.authority.text() == "已自动授权 3 台设备"
    panel.timer.stop()
    panel.shutdown()


def test_firewall_plan_is_scoped_and_maintenance_never_runs_during_session(experiment):
    from pathlib import Path

    from lab_control.agent.firewall import firewall_plan, launch_firewall

    runtime, _service, _client, _grant, _device = experiment
    settings = {"lan_enabled": True, "lan_address": "192.0.2.1", "lan_port": 8766}
    plan = firewall_plan(settings, Path(sys.executable))
    assert plan["remote_address"] == "LocalSubnet" and plan["profile"] == "Private,Domain"
    assert firewall_plan(settings, Path(sys.executable), remove=True)["rule"] == plan["rule"]
    with pytest.raises((ValueError, DeviceError, RuntimeError)):
        launch_firewall(runtime, settings)


def test_acquisition_setup_denial_does_not_initialize_shared_session(experiment, monkeypatch):
    from lab_control.domain.instruments import AcquisitionRequest

    runtime, _service, _client, _grant, device = experiment
    session = runtime.sessions.sessions[device]
    prepared = []
    acquire = session.adapter.acquire
    calls = []

    def needs_setup(request):
        calls.append(request)
        if len(calls) == 1:
            raise DeviceError("setup needed", "acquisition_setup_required")
        return acquire(request)

    monkeypatch.setattr(session.adapter, "acquire", needs_setup)
    monkeypatch.setattr(
        runtime.sessions, "_prepare_acquisition_state", lambda _session: prepared.append(True) or True
    )
    with pytest.raises(DeviceError, match="setup needed"):
        runtime.sessions.operations.acquire(session, AcquisitionRequest(kind="waveform", allow_setup=False))
    assert not prepared and len(calls) == 1
    calls.clear()
    runtime.sessions.operations.acquire(session, AcquisitionRequest(kind="waveform", allow_setup=True))
    assert prepared == [True] and len(calls) == 2


def test_failed_grant_persistence_does_not_expand_authority(experiment, monkeypatch):
    _runtime, service, client, grant, device = experiment
    before = service.policy.snapshot()

    def failed():
        raise OSError("database full")

    monkeypatch.setattr(service.policy, "_save", failed)
    with pytest.raises(OSError, match="database full"):
        service.policy.create_grant(
            client, [device], ["output_trigger"], channels=[1], operations=[], limits={}
        )
    assert service.policy.snapshot() == before
    assert "output_trigger" not in service.policy.grants[grant]["actions"]


def test_shutdown_releases_resources_after_listener_failure(experiment):
    from types import SimpleNamespace

    runtime, service, _client, _grant, _device = experiment

    def failed():
        raise OSError("listener shutdown failed")

    service.server = SimpleNamespace(close=failed)
    with pytest.raises(OSError, match="listener shutdown failed"):
        runtime.close()
    assert runtime.closed and not service.thread.is_alive() and service.worker._shutdown
    assert not runtime.sessions.sessions


def test_runtime_exit_drains_recording_and_persists_completion(experiment, tmp_path):
    runtime, service, client, grant, device = experiment
    task = service.start_recording(
        client, grant, device, {"kind": "waveform", "points": 16}, "close-record", 0.05
    )
    deadline = time.monotonic() + 5
    while not runtime.monitoring.latest(device, "waveform"):
        assert time.monotonic() < deadline
        time.sleep(0.01)
    runtime.close()
    with ApplicationRuntime(root=tmp_path, hardware=False) as reopened:
        saved = reopened.agent.ledger.get(task["id"])
        assert saved["state"] == "completed", saved
        assert saved["result"]["committed"] == saved["result"]["received"] > 0
        assert saved["result"]["converted"] == saved["result"]["committed"]
        assert reopened.agent.ledger.artifacts(task["id"])


def test_desktop_executable_dispatches_explicit_connector_mode(monkeypatch):
    from lab_control import launcher
    from lab_control.agent import connector

    calls = []
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", "C:/package/LabControl.exe")
    monkeypatch.setattr(sys, "argv", [sys.executable, "--agent-connector"])
    monkeypatch.setattr(connector, "main", lambda argv: calls.append(argv) or 2)
    assert launcher.main() == 2 and calls == [[]]
