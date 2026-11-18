"""The background service: control API + HTTP gateway on an asyncio thread.

The main thread is reserved for the UI (Tk requires it), so the network side
runs on its own thread with its own event loop. ``GuardService.start`` only
returns once both ports are bound, and raises ``AlreadyRunning`` when another
daemon owns them; that makes the port the single-instance lock.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import secrets
import threading
from typing import Optional

from aiohttp import web

from . import __version__
from .broker import ApprovalRequest, Broker, Prompter
from .config import Settings, app_dir, clear_runtime, write_runtime
from .gateway import Gateway

log = logging.getLogger("mcpguard.service")


class AlreadyRunning(RuntimeError):
    pass


class GuardService:
    def __init__(self, settings: Settings, prompter: Prompter):
        self.settings = settings
        self.prompter = prompter
        self.token = secrets.token_urlsafe(32)
        self.broker: Optional[Broker] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self._error: Optional[BaseException] = None
        self._shutdown: Optional[asyncio.Event] = None

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(target=self._thread_main, name="mcpguard-net", daemon=True)
        self._thread.start()
        self._ready.wait()
        if self._error is not None:
            if isinstance(self._error, OSError):
                raise AlreadyRunning(str(self._error))
            raise self._error

    def stop(self) -> None:
        if self._loop is not None and self._shutdown is not None:
            self._loop.call_soon_threadsafe(self._shutdown.set)
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._main())
        except BaseException as exc:  # reported to start()
            self._error = exc
            self._ready.set()

    async def _main(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._shutdown = asyncio.Event()
        self.broker = Broker(self._loop, self.prompter, self.settings.approval_timeout,
                             self.settings.max_pending, app_dir() / "audit.jsonl")

        control = web.Application(client_max_size=8 * 1024 * 1024)
        control.router.add_get("/v1/health", self._health)
        control.router.add_post("/v1/approve", self._approve)
        control.router.add_post("/v1/cancel", self._cancel)
        gateway = Gateway(self.settings, self.broker).build_app()

        runners = []
        try:
            for app, port in ((control, self.settings.control_port),
                              (gateway, self.settings.gateway_port)):
                # handler_cancellation: if a shim dies or an HTTP client hangs
                # up, the waiting handler is cancelled and the popup closes.
                runner = web.AppRunner(app, handler_cancellation=True, access_log=None)
                await runner.setup()
                runners.append(runner)
                await web.TCPSite(runner, self.settings.host, port).start()
            write_runtime(self.settings.control_port, self.token)
            self._ready.set()
            await self._shutdown.wait()
            self.broker.deny_all("MCP Guard is shutting down")
            await asyncio.sleep(0.2)  # let the denials reach their callers
        finally:
            clear_runtime()
            for runner in runners:
                await runner.cleanup()

    # -- control API (loopback only, bearer token) -------------------------

    def _authorize(self, request: web.Request) -> None:
        supplied = request.headers.get("Authorization", "")
        if not hmac.compare_digest(supplied.encode(), f"Bearer {self.token}".encode()):
            raise web.HTTPUnauthorized()

    async def _health(self, _request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "pid": os.getpid(), "version": __version__})

    async def _approve(self, request: web.Request) -> web.Response:
        self._authorize(request)
        data = await request.json()
        decision = await self.broker.decide(ApprovalRequest.from_dict(data))
        return web.json_response({"decision": decision.outcome, "reason": decision.reason})

    async def _cancel(self, request: web.Request) -> web.Response:
        self._authorize(request)
        data = await request.json()
        return web.json_response({"cancelled": self.broker.cancel(str(data.get("id")))})
