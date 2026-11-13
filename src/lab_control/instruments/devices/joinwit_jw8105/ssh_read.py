"""Read one current JW8105 display-power value through the desktop Agent."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import re
import sys
from uuid import uuid4

from lab_control.agent.connector import remote_client
from lab_control.domain.instruments import DeviceError


def target(grants, port: str, channel: int) -> tuple[str, str, bool]:
    matches = {}
    for grant in grants:
        actions = set(grant["actions"])
        if not {"view", "read_parameters"} <= actions:
            continue
        if not grant.get("full_device_access") and channel not in grant["channels"]:
            continue
        for device_id, identity in grant["devices"].items():
            if (
                identity["adapter"] == "joinwit.jw8105"
                and identity["model"].upper() == "JW8105"
                and identity["resource"].upper() == port
            ):
                can_connect = "connect" in actions
                if device_id not in matches or can_connect:
                    matches[device_id] = (grant["id"], device_id, can_connect)
    if not matches:
        raise DeviceError("没有该串口和通道的有效只读授权，请在桌面核对设备并授权", "no_matching_grant")
    if len(matches) != 1:
        raise DeviceError("多个授权资产指向同一串口，请核对设备映射", "identity_conflict")
    return next(iter(matches.values()))


async def call(client, tool: str, **arguments):
    response = await client.call_tool(tool, arguments)
    payload = response.structured_content or {}
    if response.is_error or payload.get("ok") is not True:
        raise DeviceError(payload.get("message") or f"{tool} failed", payload.get("code") or "agent_error")
    return payload["data"]


async def next_monitor_reading(client, scope: dict, state: dict, channel: int) -> dict:
    # A sequence already present before this request may be a paused or historical frame.
    start_sequence = state["monitoring"]["sequence"]
    expected_session = state["session"]["sessionId"]
    deadline = asyncio.get_running_loop().time() + 18
    while asyncio.get_running_loop().time() < deadline:
        frame = await call(
            client, "get_latest_measurement", **scope, kind="scalar", quantity="power", channel=channel
        )
        if (
            frame.get("available")
            and frame.get("session_id") == expected_session
            and frame["sequence"] > start_sequence
        ):
            readings = [
                m for m in frame["measurements"] if m["quantity"] == "power" and m["channel"] == channel
            ]
            if len(readings) != 1:
                raise DeviceError("当前监测帧未包含指定通道", "channel_not_monitored")
            reading = readings[0]
            if reading["unit"] != "dBm" or reading["points"] != 1 or len(reading["preview"]) != 1:
                raise DeviceError("监测帧未返回单通道 dBm 读数", "invalid_readback")
            return {
                "operation": "display_power",
                "source": "instrument",
                "channel": channel,
                "quality": reading["quality"],
                "value": reading["preview"][0],
                "timestamp": frame["timestamp"],
            }
        await asyncio.sleep(0.2)
    raise DeviceError("等待该通道的新监测帧超时", "timeout")


async def read_power(client, port: str, channel: int) -> dict:
    port = port.upper()
    if not re.fullmatch(r"COM[1-9][0-9]*", port) or type(channel) is not int or not 1 <= channel <= 16:
        raise DeviceError("请指定 COM 串口和 1 到 16 之间的整数通道", "invalid_parameter")
    info = await call(client, "get_server_info")
    if info.get("simulation") is not False:
        raise DeviceError("需要真实设备服务", "simulation")
    grant, device_id, can_connect = target(info["grants"], port, channel)
    scope = {"grant_id": grant, "device_id": device_id}
    state = await call(client, "get_device_state", **scope)
    if not state["session"]:
        if not can_connect:
            raise DeviceError("请先在桌面连接设备，或为该资产授予连接权限", "not_connected")
        task = await call(
            client, "connect_device", **scope, request_id=uuid4().hex, configure_acquisition=False
        )
        while task["state"] in {"accepted", "running"}:
            await asyncio.sleep(0.1)
            task = await call(client, "get_task_status", grant_id=grant, task_id=task["id"])
        if task["state"] != "completed":
            raise DeviceError(task.get("error") or "设备连接未完成", task.get("code") or "connection_failed")
    elif state["session"].get("connectionState") not in {"connected", "busy"}:
        raise DeviceError("设备会话暂不可用，请查看桌面连接状态", "session_unavailable")
    if state.get("monitoring", {}).get("running"):
        row = await next_monitor_reading(client, scope, state, channel)
    else:
        rows = await call(client, "read_parameters", **scope, operations=["display_power"], channel=channel)
        if len(rows) != 1:
            raise DeviceError("设备未返回单通道读数", "invalid_readback")
        row = rows[0]
    value = row.get("value")
    if (
        row.get("operation") != "display_power"
        or row.get("channel") != channel
        or row.get("source") != "instrument"
        or row.get("quality") != "good"
        or row.get("error")
        or not row.get("timestamp")
        or isinstance(value, bool)
        or not isinstance(value, (float, int))
        or not math.isfinite(value)
    ):
        raise DeviceError("未收到该通道的有效实测功率", "invalid_readback")
    return {
        "ok": True,
        "model": "JW8105",
        "port": port,
        "channel": channel,
        "power": value,
        "unit": "dBm",
        "timestamp": row["timestamp"],
    }


async def request(port: str, channel: int, agent_port: int) -> dict:
    if not 1 <= agent_port <= 65535:
        raise DeviceError("Agent 端口超出范围", "invalid_parameter")
    async with asyncio.timeout(25):
        async with remote_client({"url": f"http://127.0.0.1:{agent_port}/mcp"}, True) as client:
            try:
                return await read_power(client, port, channel)
            except DeviceError as exc:
                # AnyIO wraps exceptions raised from its client context in an ExceptionGroup.
                error = exc
        raise error


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True, help="Bound serial resource, e.g. COM10")
    parser.add_argument("channel", type=int, help="Optical input channel (1-16)")
    parser.add_argument("--agent-port", type=int, default=8765, help="Desktop loopback Agent port")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        result = asyncio.run(request(args.port, args.channel, args.agent_port))
    except DeviceError as exc:
        result = {"ok": False, "code": exc.code, "message": str(exc)}
    except TimeoutError:
        result = {"ok": False, "code": "timeout", "message": "等待仪器或桌面服务超时"}
    except Exception as exc:
        result = {"ok": False, "code": "connector_error", "message": f"无法完成桌面 Agent 调用：{exc}"}
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
