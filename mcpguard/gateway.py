"""HTTP gateway: intercepts remote MCP (Streamable HTTP) traffic.

Claude Web, and remote "custom connectors" in every Claude client, talk to
MCP servers from Anthropic's cloud, so nothing on the user's machine sees
that traffic unless the connector URL points at something the user runs.
This gateway is that something: a reverse proxy registered in Claude as the
connector URL (through an HTTPS tunnel). It forwards everything to the real
upstream server, except that guarded requests wait for the user's decision.

URL layout: ``/mcp/<gateway_secret>/<upstream_name>``. The secret segment
keeps strangers who discover the tunnel hostname from reaching the upstream
or spamming the user with popups.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
from typing import Any, List

import aiohttp
from aiohttp import web

from .broker import ApprovalRequest, Broker
from .config import Settings
from .protocol import DENIED_CODE, denial, is_guarded, summarize

log = logging.getLogger("mcpguard.gateway")

REQUEST_HEADERS = ("accept", "content-type", "authorization", "mcp-session-id",
                   "mcp-protocol-version", "last-event-id")
RESPONSE_HEADERS = ("content-type", "mcp-session-id", "www-authenticate",
                    "cache-control", "allow")
MAX_BODY = 8 * 1024 * 1024


class Gateway:
    def __init__(self, settings: Settings, broker: Broker):
        self.settings = settings
        self.broker = broker
        self._session: aiohttp.ClientSession | None = None

    def build_app(self) -> web.Application:
        app = web.Application(client_max_size=MAX_BODY)
        app.router.add_route("*", "/mcp/{secret}/{name}", self.handle)
        app.on_startup.append(self._startup)
        app.on_cleanup.append(self._cleanup)
        return app

    async def _startup(self, _app) -> None:
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=15)
        self._session = aiohttp.ClientSession(timeout=timeout)

    async def _cleanup(self, _app) -> None:
        if self._session is not None:
            await self._session.close()

    # ------------------------------------------------------------------

    async def handle(self, request: web.Request) -> web.StreamResponse:
        secret = request.match_info["secret"].encode()
        if not hmac.compare_digest(secret, self.settings.gateway_secret.encode()):
            raise web.HTTPNotFound()
        name = request.match_info["name"]
        upstream = self.settings.upstreams.get(name)
        if not upstream:
            raise web.HTTPNotFound()
        if request.method not in ("GET", "POST", "DELETE"):
            raise web.HTTPMethodNotAllowed(request.method, ["GET", "POST", "DELETE"])

        body = await request.read() if request.method == "POST" else None
        if body:
            blocked = await self._gate(name, body)
            if blocked is not None:
                return blocked
        return await self._forward(request, upstream, body)

    async def _gate(self, name: str, body: bytes):
        """Return a denial response, or ``None`` when the body may be forwarded."""
        try:
            payload = json.loads(body)
        except ValueError:
            return None  # not JSON-RPC; let the upstream reject it
        messages: List[Any] = payload if isinstance(payload, list) else [payload]
        guarded = [m for m in messages if is_guarded(m, self.settings.guarded_methods)]
        if not guarded:
            return None

        async def ask(msg):
            tool, arguments = summarize(msg)
            return await self.broker.decide(ApprovalRequest(
                source="Claude Web / remote connector (HTTP gateway)",
                server=name, method=msg["method"], tool=tool, arguments=arguments))

        # Simultaneous calls in one POST each get their own prompt.
        decisions = await asyncio.gather(*(ask(m) for m in guarded))
        if all(d.allowed for d in decisions):
            return None

        # A batch is all or nothing: if any guarded call is blocked, nothing in
        # the POST reaches the upstream.
        reasons = {id(m): d for m, d in zip(guarded, decisions)}
        replies = []
        for msg in messages:
            if not isinstance(msg, dict) or "id" not in msg:
                continue
            decision = reasons.get(id(msg))
            if decision is not None and not decision.allowed:
                replies.append(denial(msg, decision.reason, self.settings.deny_mode))
            else:
                replies.append({"jsonrpc": "2.0", "id": msg["id"], "error": {
                    "code": DENIED_CODE,
                    "message": "Blocked by MCP Guard: another call in this batch was blocked"}})
        if not replies:
            return web.Response(status=202)
        return web.json_response(replies if isinstance(payload, list) else replies[0])

    async def _forward(self, request: web.Request, upstream: dict, body) -> web.StreamResponse:
        headers = {k: request.headers[k] for k in REQUEST_HEADERS if k in request.headers}
        headers["Accept-Encoding"] = "identity"  # bytes are relayed untouched
        headers.update(upstream.get("headers") or {})  # e.g. a static API key
        assert self._session is not None
        response = None
        try:
            async with self._session.request(request.method, upstream["url"],
                                             headers=headers, data=body) as up:
                response = web.StreamResponse(status=up.status)
                for key in RESPONSE_HEADERS:
                    if key in up.headers:
                        response.headers[key] = up.headers[key]
                await response.prepare(request)
                # Streams JSON bodies and long-lived SSE streams alike.
                async for chunk in up.content.iter_any():
                    await response.write(chunk)
                await response.write_eof()
                return response
        except aiohttp.ClientError as exc:
            log.warning("upstream %s failed: %s", upstream.get("url"), exc)
            if response is not None and response.prepared:
                return response  # failed mid-stream; nothing more can be sent
            return web.json_response(
                {"jsonrpc": "2.0", "id": None, "error": {
                    "code": -32000, "message": "MCP Guard: upstream MCP server unreachable"}},
                status=502)
