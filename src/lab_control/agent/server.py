"""Loopback MCP listener attached to one desktop runtime."""

from __future__ import annotations

import asyncio
import json
import socket
import threading
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from mcp.server.transport_security import RequestBodyLimitMiddleware, TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Mount, Route

from lab_control.agent.tools import create_mcp_server
from lab_control.application.agents.policy import INTERNAL_CLIENT
from lab_control.domain.instruments import DeviceError

DEFAULTS = {"enabled": False, "local_port": 8765}


def saved_settings(repository):
    saved = repository.load_workspace("agent/server", {})
    port = saved.get("local_port")
    return {
        "enabled": saved.get("enabled") is True,
        "local_port": port if type(port) is int and 1024 <= port <= 65535 else DEFAULTS["local_port"],
    }


class RequestGuard:
    def __init__(self, app, authority: str, scheme: str):
        self.app, self.authority, self.scheme = app, authority, scheme
        self.inflight = 0

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request = Request(scope, receive)
        if request.headers.get("host") != self.authority or request.headers.get("origin") not in {
            None,
            f"{self.scheme}://{self.authority}",
        }:
            return await JSONResponse({"error": "invalid_host_or_origin"}, status_code=403)(
                scope, receive, send
            )
        if self.inflight >= 32:
            return await JSONResponse({"error": "server_busy"}, status_code=429)(scope, receive, send)
        self.inflight += 1
        try:
            return await self.app(scope, receive, send)
        finally:
            self.inflight -= 1


def create_app(service, port: int):
    server = create_mcp_server(service)
    authority, scheme = f"127.0.0.1:{port}", "http"
    mcp_app = server.streamable_http_app(
        stateless_http=True,
        json_response=True,
        max_request_body_size=1024 * 1024,
        transport_security=TransportSecuritySettings(
            allowed_hosts=[authority], allowed_origins=[f"{scheme}://{authority}"]
        ),
    )

    @asynccontextmanager
    async def lifespan(_app):
        async with mcp_app.router.lifespan_context(mcp_app):
            yield

    async def download(request):
        try:
            value, path = service.artifact_path(
                INTERNAL_CLIENT, request.query_params.get("grant_id", ""), request.path_params["artifact_id"]
            )
            return FileResponse(
                path, filename=Path(value["name"]).name, headers={"Cache-Control": "no-store"}
            )
        except DeviceError as exc:
            return JSONResponse({"error": exc.code}, status_code=403)

    app = Starlette(
        routes=[Route("/artifacts/{artifact_id}", download), Mount("/", app=mcp_app)], lifespan=lifespan
    )
    return RequestGuard(RequestBodyLimitMiddleware(app, 1024 * 1024), authority, scheme)


class _Listener:
    def __init__(self, service, port):
        self.port = port
        self.ready = threading.Event()
        self.error = ""
        self.server = uvicorn.Server(
            uvicorn.Config(
                create_app(service, port),
                host="127.0.0.1",
                port=port,
                access_log=False,
                log_config=None,
                timeout_graceful_shutdown=5,
            )
        )
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            self.sock.bind(("127.0.0.1", port))
            self.sock.listen(64)
            self.sock.setblocking(False)
            self.thread = threading.Thread(target=self._run, name="labcontrol-mcp", daemon=True)
            self.thread.start()
        except OSError as exc:
            self.sock.close()
            raise OSError(f"无法监听本机端口 {port}：{exc}") from exc
        except Exception:
            self.sock.close()
            raise
        if not self.ready.wait(10) or not self.running:
            self.close()
            raise RuntimeError(self.error or f"MCP 端口 {port} 启动失败")

    @property
    def running(self):
        return self.server.started and self.thread.is_alive() and not self.error

    def _run(self):
        async def serve():
            task = asyncio.create_task(self.server.serve(sockets=[self.sock]))
            while not self.server.started and not task.done():
                await asyncio.sleep(0.01)
            self.ready.set()
            await task

        try:
            asyncio.run(serve())
        except Exception as exc:
            self.error = str(exc)
        finally:
            self.ready.set()

    def close(self):
        self.server.should_exit = True
        self.thread.join(timeout=15)
        if self.thread.is_alive():
            raise RuntimeError("MCP 服务仍在退出，请等待在途请求完成")
        self.sock.close()


class AgentServer:
    def __init__(self, service):
        self.service = service
        self.settings = saved_settings(service.repository)
        self.listener: _Listener | None = None
        self.lock = threading.Lock()

    def configure(self, settings):
        port = settings.get("local_port", DEFAULTS["local_port"])
        if type(port) is not int or not 1024 <= port <= 65535:
            raise ValueError("本机端口须为 1024–65535")
        candidate = {"enabled": settings.get("enabled") is True, "local_port": port}
        with self.lock:
            old = self.listener
            new = None
            if candidate["enabled"]:
                new = old if old and old.port == port and old.running else _Listener(self.service, port)
            try:
                self.service.repository.save_workspace("agent/server", candidate)
            except Exception:
                if new and new is not old:
                    new.close()
                raise
            self.settings = candidate
            self.listener = new
            if old and old is not new:
                old.close()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.settings['local_port']}/mcp"

    def snapshot(self):
        listener = self.listener
        return {
            "running": bool(listener and listener.running),
            "settings": dict(self.settings),
            "error": listener.error if listener else "",
        }

    def export_connection(self, destination: Path):
        stream = destination.open("x", encoding="utf-8")
        try:
            with stream:
                json.dump({"schema_version": 1, "url": self.url}, stream, ensure_ascii=False, indent=2)
        except Exception:
            destination.unlink()
            raise

    def close(self):
        with self.lock:
            if self.listener:
                self.listener.close()
                self.listener = None
