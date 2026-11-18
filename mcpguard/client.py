"""Client side of the control API, used by the stdio shim.

Standard library only (the shim starts once per MCP server, so it should be
light) and strictly fail closed: if the daemon cannot be reached and cannot
be started, the answer is "deny".
"""

from __future__ import annotations

import http.client
import json
import os
import subprocess
import sys
import time
from typing import Any, Dict, Tuple

from .broker import DENY
from .config import Settings, app_dir, read_runtime


class DaemonClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    # -- plumbing -----------------------------------------------------------

    def _call(self, method: str, path: str, body: Any = None, timeout: float = 3.0) -> Dict[str, Any]:
        runtime = read_runtime() or {}
        conn = http.client.HTTPConnection(
            self.settings.host, int(runtime.get("port") or self.settings.control_port),
            timeout=timeout)
        try:
            headers = {"Authorization": f"Bearer {runtime.get('token', '')}"}
            data = None
            if body is not None:
                data = json.dumps(body).encode()
                headers["Content-Type"] = "application/json"
            conn.request(method, path, body=data, headers=headers)
            response = conn.getresponse()
            raw = response.read()
            if response.status != 200:
                raise OSError(f"daemon answered HTTP {response.status}")
            return json.loads(raw)
        finally:
            conn.close()

    def alive(self) -> bool:
        try:
            return bool(self._call("GET", "/v1/health", timeout=1.0).get("ok"))
        except (OSError, ValueError, http.client.HTTPException):
            return False

    def ensure_daemon(self, wait: float = 10.0) -> bool:
        """Make sure a daemon is running, starting one in the background if needed."""
        if self.alive():
            return True
        if os.environ.get("MCPGUARD_NO_AUTOSTART"):
            return False
        log = open(app_dir() / "daemon.log", "ab")
        kwargs: Dict[str, Any] = dict(stdin=subprocess.DEVNULL, stdout=log, stderr=log)
        if os.name == "nt":
            kwargs["creationflags"] = (subprocess.CREATE_NEW_PROCESS_GROUP
                                       | getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            kwargs["start_new_session"] = True  # survive Claude Desktop restarts
        try:
            # If several shims race, the losers fail to bind the port and exit.
            subprocess.Popen([sys.executable, "-m", "mcpguard", "daemon"], **kwargs)
        except OSError:
            return False
        finally:
            log.close()
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if self.alive():
                return True
            time.sleep(0.25)
        return False

    # -- API ----------------------------------------------------------------

    def request_approval(self, payload: Dict[str, Any]) -> Tuple[str, str]:
        """Block until the user decides. Returns ``(outcome, reason)``."""
        if not self.ensure_daemon():
            return DENY, "the MCP Guard daemon is not running, so nobody could approve this"
        try:
            # The daemon enforces the real timeout; this is only a safety net.
            answer = self._call("POST", "/v1/approve", payload,
                                timeout=self.settings.approval_timeout + 15)
            return str(answer["decision"]), str(answer.get("reason", ""))
        except (OSError, ValueError, KeyError, http.client.HTTPException) as exc:
            return DENY, f"lost contact with the MCP Guard daemon ({exc})"

    def cancel(self, approval_id: str) -> None:
        try:
            self._call("POST", "/v1/cancel", {"id": approval_id})
        except (OSError, ValueError, http.client.HTTPException):
            pass
