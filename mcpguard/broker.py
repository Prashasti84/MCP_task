"""The approval broker: one pending future per MCP request.

The broker lives on the daemon's asyncio loop. Interceptors (the control API
used by stdio shims, and the HTTP gateway) call :meth:`Broker.decide` and are
suspended until the user answers, the request times out, or the caller goes
away. The UI runs on another thread and answers through
:meth:`Broker.resolve_threadsafe`.

Every path that is not an explicit "Allow" click ends in a denial
(fail closed).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

log = logging.getLogger("mcpguard.broker")

ALLOW, DENY, CANCELLED = "allow", "deny", "cancelled"


@dataclass
class ApprovalRequest:
    source: str                 # e.g. "Claude Desktop (stdio)"
    server: str                 # MCP server name
    method: str                 # JSON-RPC method, e.g. "tools/call"
    tool: str                   # tool name (or method for non-tool requests)
    arguments: Any = None       # request parameters
    description: str = ""       # tool description, when known
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created: float = field(default_factory=time.time)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ApprovalRequest":
        req = cls(
            source=str(data.get("source", "unknown"))[:200],
            server=str(data.get("server", "unknown"))[:200],
            method=str(data.get("method", "unknown"))[:200],
            tool=str(data.get("tool", "unknown"))[:500],
            arguments=data.get("arguments"),
            description=str(data.get("description") or "")[:2000],
        )
        if isinstance(data.get("id"), str) and data["id"]:
            req.id = data["id"][:64]
        return req


@dataclass
class Decision:
    outcome: str
    reason: str = ""

    @property
    def allowed(self) -> bool:
        return self.outcome == ALLOW


class Prompter:
    """Interface implemented by UIs. Methods may be called from any thread."""

    def bind(self, resolver) -> None:  # resolver(request_id, allow, reason)
        self._resolver = resolver

    def show(self, request: ApprovalRequest, timeout: float) -> None:
        raise NotImplementedError

    def dismiss(self, request_id: str) -> None:
        pass

    def run(self) -> None:
        """Block the main thread until :meth:`stop` is called."""
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError


class Broker:
    def __init__(self, loop: asyncio.AbstractEventLoop, prompter: Prompter,
                 timeout: float, max_pending: int, audit_path: Optional[Path] = None):
        self._loop = loop
        self._prompter = prompter
        self._timeout = timeout
        self._max_pending = max_pending
        self._audit_path = audit_path
        self._pending: Dict[str, asyncio.Future] = {}
        self._closing = False
        prompter.bind(self.resolve_threadsafe)

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    async def decide(self, request: ApprovalRequest) -> Decision:
        """Hold the caller until a decision exists for ``request``."""
        if self._closing:
            return self._finish(request, Decision(DENY, "MCP Guard is shutting down"))
        if len(self._pending) >= self._max_pending:
            return self._finish(request, Decision(
                DENY, f"too many requests awaiting approval ({self._max_pending})"))
        if request.id in self._pending:
            return self._finish(request, Decision(DENY, "duplicate approval id"))

        future: asyncio.Future = self._loop.create_future()
        self._pending[request.id] = future
        try:
            self._prompter.show(request, self._timeout)
            decision = await asyncio.wait_for(future, self._timeout)
        except asyncio.TimeoutError:
            decision = Decision(DENY, f"no answer within {int(self._timeout)}s")
        except asyncio.CancelledError:
            # The requester disconnected (shim died, HTTP client gave up).
            self._finish(request, Decision(CANCELLED, "requester went away"))
            raise
        except Exception as exc:  # UI failure must never allow a request
            log.exception("prompt failed")
            decision = Decision(DENY, f"approval UI error: {exc}")
        finally:
            self._pending.pop(request.id, None)
            try:
                self._prompter.dismiss(request.id)
            except Exception:
                log.exception("dismiss failed")
        return self._finish(request, decision)

    # -- resolution -------------------------------------------------------

    def resolve_threadsafe(self, request_id: str, allow: bool, reason: str = "") -> None:
        decision = Decision(ALLOW if allow else DENY,
                            reason or ("approved by user" if allow else "blocked by user"))
        self._loop.call_soon_threadsafe(self._resolve, request_id, decision)

    def cancel(self, request_id: str) -> bool:
        return self._resolve(request_id, Decision(CANCELLED, "cancelled by client"))

    def deny_all(self, reason: str) -> None:
        self._closing = True
        for request_id in list(self._pending):
            self._resolve(request_id, Decision(DENY, reason))

    def _resolve(self, request_id: str, decision: Decision) -> bool:
        future = self._pending.get(request_id)
        if future is None or future.done():
            return False
        future.set_result(decision)
        return True

    # -- audit ------------------------------------------------------------

    def _finish(self, request: ApprovalRequest, decision: Decision) -> Decision:
        log.info("%s %s/%s from %s (%s)", decision.outcome.upper(), request.server,
                 request.tool, request.source, decision.reason)
        if self._audit_path is not None:
            entry = asdict(request)
            # Parameter values can be sensitive; the audit keeps only the keys.
            args = entry.pop("arguments")
            entry["argument_keys"] = sorted(args) if isinstance(args, dict) else []
            entry.update(decision=decision.outcome, reason=decision.reason,
                         decided=time.time())
            try:
                with open(self._audit_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(entry) + "\n")
            except OSError:
                log.exception("cannot write audit log")
        return decision
