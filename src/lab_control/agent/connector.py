"""Stdio/HTTPS bridge and grant-scoped artifact download; never opens hardware."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import ssl
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlencode, urlsplit

import httpx2
import mcp.types as types
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from pydantic import BaseModel, ConfigDict, Field

FRAME_TIMEOUT = 600
MAX_FRAME_BYTES = 512 * 1024 * 1024


class DownloadFrame(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    grant_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    task_id: str = Field(min_length=1, max_length=128)
    directory: str = Field(min_length=1, max_length=32767)


def connection(path: Path) -> tuple[dict, ssl.SSLContext | bool]:
    config = json.loads(path.read_text(encoding="utf-8-sig"))
    url = urlsplit(config["url"])
    if config.get("schema_version") != 1 or url.path != "/mcp" or url.query or url.fragment or url.username:
        raise ValueError("Invalid connection configuration")
    if url.scheme == "https":
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_verify_locations(cadata=config["certificate_pem"])
        verify: ssl.SSLContext | bool = context
    elif url.scheme == "http" and url.hostname == "127.0.0.1":
        verify = True
    else:
        raise ValueError("Remote connections require HTTPS with the exported server certificate")
    return config, verify


@asynccontextmanager
async def remote_client(config, verify):
    async with httpx2.AsyncClient(
        verify=verify,
        trust_env=False,
        follow_redirects=False,
        timeout=70,
    ) as http:

        @asynccontextmanager
        async def transport():
            async with streamable_http_client(config["url"], http_client=http) as streams:
                yield streams[0], streams[1]

        async with Client(transport(), read_timeout_seconds=70) as client:
            yield client


async def client_call(client, config, verify, name, arguments):
    from lab_control.storage.frame_files import directory, filename, publish

    remote_save = (
        name == "save_frame" and arguments.get("destination") == "client" and arguments.get("directory")
    )
    if name != "download_frame" and not remote_save:
        return await client.call_tool(name, arguments)
    try:
        if name == "download_frame":
            arguments = DownloadFrame.model_validate(arguments).model_dump()
        export_root = Path(config.get("export_root", Path.home() / "Downloads" / "LabControl"))
        folder = directory(arguments["directory"], export_root)
        grant = arguments["grant_id"]
        if remote_save:
            submitted = await client.call_tool(name, {**arguments, "directory": ""})
            if submitted.is_error:
                return submitted
            task_id = submitted.structured_content["data"]["id"]
        else:
            task_id = arguments["task_id"]
        try:
            async with asyncio.timeout(FRAME_TIMEOUT):
                while True:
                    status = await client.call_tool(
                        "get_task_status", {"grant_id": grant, "task_id": task_id}
                    )
                    if status.is_error:
                        return status
                    task = status.structured_content["data"]
                    if task["id"] != task_id:
                        raise ValueError("Unexpected task identity")
                    if task["state"] not in {"accepted", "running", "stopping"}:
                        break
                    await asyncio.sleep(0.25)
        except TimeoutError as exc:
            raise TimeoutError("等待单帧超时，可按原任务号重新下载") from exc
        result = task["result"]
        if (
            task["state"] != "completed"
            or result.get("operation") != "save_frame"
            or result["destination"] != "client"
        ):
            raise ValueError(task.get("error") or "Task has no completed client frame")
        stem = filename(result["stem"])
        expected = {"both": ["png", "csv"], "png": ["png"], "csv": ["csv"]}[result["mode"]]
        if [f["format"] for f in result["files"]] != expected:
            raise ValueError("Incomplete frame manifest")
        if any(type(f["size"]) is not int or not 0 < f["size"] <= MAX_FRAME_BYTES for f in result["files"]):
            raise ValueError("Invalid frame artifact size")
        directory(str(folder), export_root).mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(dir=folder, prefix=".frame-") as stage:
            staged = {}
            for info in result["files"]:
                ext = info["format"]
                path = Path(stage) / ("frame." + ext)
                await asyncio.to_thread(
                    download, config, verify, info["artifact_id"], grant, path, max_bytes=info["size"]
                )
                with path.open("rb") as stream:
                    if (
                        path.stat().st_size != info["size"]
                        or hashlib.file_digest(stream, "sha256").hexdigest() != info["sha256"]
                    ):
                        raise ValueError("Incomplete frame download")
                staged[ext] = path
            last = await client.call_tool(
                "get_artifact", {"grant_id": grant, "artifact_id": info["artifact_id"]}
            )
            if last.is_error:
                return last
            targets = publish(folder, stem, staged, check=lambda: directory(str(folder), export_root))
        for info in result["files"]:
            info["path"] = str(targets[info["format"]])
        payload = {"ok": True, "data": task}
    except (OSError, ValueError, TypeError, KeyError, httpx2.HTTPError) as exc:
        payload = {"ok": False, "message": str(exc), "task_id": locals().get("task_id", "")}
    return types.CallToolResult(
        is_error=not payload["ok"],
        structured_content=payload,
        content=[types.TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))],
    )


async def bridge(config, verify, *, check=False):
    async with remote_client(config, verify) as client:
        if check:
            result = await client.call_tool("get_server_info", {})
            print(result.model_dump_json(by_alias=True))
            return 1 if result.is_error else 0

        async def list_tools(_context, params):
            listing = await client.list_tools(cursor=params.cursor if params else None, cache_mode="refresh")
            if any(tool.name == "save_frame" for tool in listing.tools):
                listing.tools.append(
                    types.Tool(
                        name="download_frame",
                        description="下载原单帧任务到本机目录，可在传输中断后重试。",
                        input_schema=DownloadFrame.model_json_schema(),
                    )
                )
            return listing

        async def call_tool(_context, params):
            # The remote service owns accepted task lifetimes and request IDs.
            # Losing this stdio connection must not cancel its device work.
            return await client_call(client, config, verify, params.name, params.arguments or {})

        server = Server(
            "LabControl connector", version="1.0", on_list_tools=list_tools, on_call_tool=call_tool
        )
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
    return 0


async def call_once(config, verify, name, arguments):
    async with remote_client(config, verify) as client:
        result = await client_call(client, config, verify, name, arguments)
        payload = result.structured_content
        if payload is None:
            payload = {"ok": not result.is_error, "content": [item.model_dump() for item in result.content]}
        print(json.dumps(payload, ensure_ascii=False, allow_nan=False))
        return 1 if result.is_error else 0


def download(config, verify, artifact: str, grant: str, output: Path, *, max_bytes: int | None = None):
    if not re.fullmatch(r"[a-f0-9]{32}", artifact) or not re.fullmatch(r"[a-f0-9]{32}", grant):
        raise ValueError("Invalid artifact or grant ID")
    url = config["url"].removesuffix("/mcp") + f"/artifacts/{artifact}?" + urlencode({"grant_id": grant})
    # Exclusive creation protects existing experiment files. A failed partial
    # download remains clearly named and is never presented as a complete result.
    if output.exists():
        raise FileExistsError(output)
    partial = output.with_name(output.name + ".partial")
    with httpx2.Client(
        verify=verify,
        trust_env=False,
        follow_redirects=False,
        timeout=70,
    ) as client:
        with client.stream("GET", url) as response:
            response.raise_for_status()
            expected = response.headers.get("content-length")
            if max_bytes is not None and expected is not None and int(expected) > max_bytes:
                raise ValueError("Artifact exceeds download limit")
            count = 0
            with partial.open("xb") as stream:
                for chunk in response.iter_bytes():
                    count += len(chunk)
                    if max_bytes is not None and count > max_bytes:
                        raise ValueError("Artifact exceeds download limit")
                    stream.write(chunk)
            if expected is not None and count != int(expected):
                raise OSError("Incomplete artifact download; partial file retained")
    # Windows rename refuses to replace an existing destination.
    partial.rename(output)
    return {"path": str(output.resolve()), "bytes": count}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="LabControl MCP connector (no instrument access)")
    parser.add_argument("--connection", required=True, type=Path)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--check", action="store_true")
    action.add_argument("--download", metavar="ARTIFACT_ID")
    action.add_argument("--call", metavar="TOOL")
    parser.add_argument(
        "--arguments-file", type=Path, help="JSON object file; '-' reads UTF-8 JSON from stdin"
    )
    parser.add_argument("--grant")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.call and hasattr(sys.stdout, "reconfigure"):
        # Windows redirected stdout otherwise follows the login's code page,
        # which need not match the UTF-8 JSON expected by the SSH caller.
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        config, verify = connection(args.connection)
        if args.arguments_file and not args.call:
            raise ValueError("--arguments-file requires --call")
        if args.call:
            if args.arguments_file is None:
                arguments = {}
            else:
                if str(args.arguments_file) == "-":
                    content = sys.stdin.buffer.read(1024 * 1024 + 1)
                else:
                    with args.arguments_file.open("rb") as stream:
                        content = stream.read(1024 * 1024 + 1)
                if len(content) > 1024 * 1024:
                    raise ValueError("Tool arguments exceed 1 MiB")
                arguments = json.loads(content.decode("utf-8-sig"))
                if not isinstance(arguments, dict):
                    raise ValueError("Tool arguments must be a JSON object")
            return asyncio.run(call_once(config, verify, args.call, arguments))
        if args.download:
            if not args.grant or not args.output:
                parser.error("--download requires --grant and --output")
            print(
                json.dumps(
                    download(config, verify, args.download, args.grant, args.output),
                    ensure_ascii=False,
                )
            )
            return 0
        return asyncio.run(bridge(config, verify, check=args.check))
    except Exception as exc:
        if args.call:
            print(
                json.dumps({"ok": False, "code": "connector_error", "message": str(exc)}, ensure_ascii=False)
            )
            return 2
        print(f"Agent connector: {exc}", file=sys.stderr)
        return 2
