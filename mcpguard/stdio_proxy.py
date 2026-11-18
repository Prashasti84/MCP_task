"""Stdio shim: sits between Claude Desktop and one local MCP server.

Claude Desktop starts local MCP servers as child processes and speaks
newline-delimited JSON-RPC over their stdin/stdout. After ``mcpguard
install`` Claude Desktop starts this shim instead, and the shim starts the
real server::

    Claude Desktop <--stdio--> shim <--stdio--> real MCP server
                                 |
                                 +--HTTP (loopback)--> daemon --> popup

Every message is relayed byte for byte, except guarded requests
(``tools/call`` by default), which are parked on their own thread until the
daemon returns the user's decision. Parking a call never blocks the relay,
so other traffic and other calls keep flowing.

Threads are used rather than asyncio because asyncio cannot read stdin pipes
portably on Windows.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
import uuid
from typing import Any, Dict, List

from .broker import ALLOW, CANCELLED
from .client import DaemonClient
from .config import Settings
from .protocol import denial, is_guarded, summarize


def _log(*parts: Any) -> None:
    # stdout belongs to the protocol; diagnostics go to stderr, which Claude
    # Desktop stores in its per-server MCP log.
    print("[mcpguard]", *parts, file=sys.stderr, flush=True)


class StdioProxy:
    def __init__(self, name: str, command: List[str], settings: Settings,
                 client: DaemonClient, source: str = "Claude Desktop (stdio)"):
        self.name = name
        self.command = command
        self.settings = settings
        self.client = client
        self.source = source
        self._to_client_lock = threading.Lock()
        self._to_server_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._pending: Dict[str, str] = {}        # request id -> approval id
        self._tool_list_ids: set = set()          # ids of in-flight tools/list
        self._tool_descriptions: Dict[str, str] = {}
        self._child: subprocess.Popen

    # -- writing ------------------------------------------------------------

    def _to_client(self, data: bytes) -> None:
        with self._to_client_lock:
            try:
                sys.stdout.buffer.write(data)
                sys.stdout.buffer.flush()
            except (OSError, ValueError):
                pass  # Claude Desktop is gone; the main loop will wind down

    def _to_server(self, data: bytes) -> None:
        with self._to_server_lock:
            try:
                self._child.stdin.write(data)
                self._child.stdin.flush()
            except (OSError, ValueError):
                pass  # server died; its exit is handled in run()

    @staticmethod
    def _encode(msg: Any) -> bytes:
        return json.dumps(msg, separators=(",", ":")).encode() + b"\n"

    # -- client -> server ---------------------------------------------------

    def _pump_client(self) -> None:
        for line in iter(sys.stdin.buffer.readline, b""):
            try:
                self._handle_client_line(line)
            except Exception as exc:  # never let one bad message kill the relay
                _log("error handling client message:", repr(exc))
        # Claude Desktop closed our stdin: ask the server to exit the same way.
        try:
            self._child.stdin.close()
        except OSError:
            pass

    def _handle_client_line(self, line: bytes) -> None:
        try:
            payload = json.loads(line)
        except ValueError:
            return self._to_server(line)  # not ours to judge
        if isinstance(payload, list):
            # JSON-RPC batch (dropped from MCP in 2025-06-18): handle per item.
            if any(is_guarded(m, self.settings.guarded_methods) for m in payload):
                for item in payload:
                    self._handle_client_message(item, self._encode(item))
                return
            return self._to_server(line)
        self._handle_client_message(payload, line)

    def _handle_client_message(self, msg: Any, raw: bytes) -> None:
        if not isinstance(msg, dict):
            return self._to_server(raw)
        method = msg.get("method")

        if is_guarded(msg, self.settings.guarded_methods):
            if "id" not in msg:
                _log("dropped guarded notification", method)  # cannot be answered: fail closed
                return
            threading.Thread(target=self._gate, args=(msg, raw), daemon=True,
                             name=f"gate-{msg['id']}").start()
            return

        if method == "notifications/cancelled":
            key = json.dumps((msg.get("params") or {}).get("requestId"))
            with self._state_lock:
                approval_id = self._pending.get(key)
            if approval_id:
                # Still waiting for the user: close the popup. The server
                # never saw the request, so the notification stops here.
                self.client.cancel(approval_id)
                return

        if method == "tools/list" and "id" in msg:
            with self._state_lock:
                self._tool_list_ids.add(json.dumps(msg["id"]))
        self._to_server(raw)

    def _gate(self, msg: Dict[str, Any], raw: bytes) -> None:
        """Hold one guarded request until the user decides."""
        key = json.dumps(msg["id"])
        approval_id = uuid.uuid4().hex
        tool, arguments = summarize(msg)
        with self._state_lock:
            duplicate = key in self._pending
            if not duplicate:
                self._pending[key] = approval_id
            description = self._tool_descriptions.get(tool, "")
        if duplicate:
            return self._to_client(self._encode(denial(
                msg, "a request with this id is already awaiting approval",
                self.settings.deny_mode)))
        try:
            outcome, reason = self.client.request_approval({
                "id": approval_id, "source": self.source, "server": self.name,
                "method": msg["method"], "tool": tool, "arguments": arguments,
                "description": description,
            })
        except Exception as exc:  # defensive: any failure means "deny"
            outcome, reason = "deny", f"internal error ({exc!r})"
        finally:
            with self._state_lock:
                self._pending.pop(key, None)

        if outcome == ALLOW:
            self._to_server(raw)
        elif outcome == CANCELLED:
            pass  # Claude cancelled the request; MCP expects no response
        else:
            _log(f"blocked {tool}: {reason}")
            self._to_client(self._encode(denial(msg, reason, self.settings.deny_mode)))

    # -- server -> client ---------------------------------------------------

    def _pump_server(self) -> None:
        for line in iter(self._child.stdout.readline, b""):
            if self._tool_list_ids:
                self._learn_tools(line)
            self._to_client(line)

    def _learn_tools(self, line: bytes) -> None:
        """Remember tool descriptions from tools/list so popups can show them."""
        try:
            msg = json.loads(line)
            key = json.dumps(msg.get("id"))
            with self._state_lock:
                if key not in self._tool_list_ids:
                    return
                self._tool_list_ids.discard(key)
                for tool in msg["result"]["tools"]:
                    self._tool_descriptions[str(tool["name"])] = str(tool.get("description") or "")
        except (ValueError, KeyError, TypeError, AttributeError):
            pass

    # -- lifecycle ----------------------------------------------------------

    def run(self) -> int:
        executable = shutil.which(self.command[0]) or self.command[0]  # finds npx.cmd on Windows
        try:
            self._child = subprocess.Popen([executable, *self.command[1:]],
                                           stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                           bufsize=0)
        except OSError as exc:
            _log(f"cannot start MCP server {self.command!r}: {exc}")
            return 127

        # Start the daemon early so the first popup is not delayed.
        threading.Thread(target=self.client.ensure_daemon, daemon=True).start()

        finished = threading.Event()
        client_closed = threading.Event()

        def run_pump(pump, flag=None) -> None:
            try:
                pump()
            finally:
                if flag is not None:
                    flag.set()
                finished.set()

        threading.Thread(target=run_pump, args=(self._pump_client, client_closed),
                         daemon=True, name="client-pump").start()
        server_pump = threading.Thread(target=run_pump, args=(self._pump_server,),
                                       daemon=True, name="server-pump")
        server_pump.start()
        try:
            # Ends when Claude Desktop closes our stdin or the server exits.
            finished.wait()
            if client_closed.is_set():
                try:
                    self._child.wait(timeout=5)  # let the server exit on its own
                except subprocess.TimeoutExpired:
                    pass
        except KeyboardInterrupt:
            pass
        finally:
            self._shutdown()
        server_pump.join(timeout=2)  # flush the server's last output
        return self._child.returncode or 0

    def _shutdown(self) -> None:
        with self._state_lock:
            waiting = list(self._pending.values())
        for approval_id in waiting:  # close popups nobody can use any more
            self.client.cancel(approval_id)
        if self._child.poll() is None:
            _log("stopping MCP server")
            self._child.terminate()
            try:
                self._child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._child.kill()
                self._child.wait()
