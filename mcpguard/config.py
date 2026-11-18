"""Settings, on-disk locations and the daemon runtime file.

Everything lives in one private directory (default ``~/.mcpguard``, override
with the ``MCPGUARD_HOME`` environment variable):

* ``config.json``   user settings (ports, timeout, gateway upstreams, secret)
* ``runtime.json``  written by the running daemon: pid, port and the bearer
                    token that stdio shims must present
* ``audit.jsonl``   one line per decision
* ``daemon.log``    daemon output when it is auto-started by a shim
"""

from __future__ import annotations

import json
import os
import secrets
import tempfile
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Dict, List, Optional


def app_dir() -> Path:
    path = Path(os.environ.get("MCPGUARD_HOME") or (Path.home() / ".mcpguard"))
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def write_private(path: Path, text: str) -> None:
    """Atomically write a file readable only by the current user."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@dataclass
class Settings:
    host: str = "127.0.0.1"
    #: Local control API used by stdio shims. Never expose this port.
    control_port: int = 8765
    #: HTTP gateway for remote connectors (Claude Web). Expose via a tunnel.
    gateway_port: int = 8766
    #: Seconds to wait for the user before a request is blocked automatically.
    approval_timeout: float = 120.0
    #: Upper bound on simultaneously open prompts (popup flood protection).
    max_pending: int = 25
    #: JSON-RPC methods that need approval.
    guarded_methods: List[str] = field(default_factory=lambda: ["tools/call"])
    #: "tool_result" (isError result the model can read) or "jsonrpc_error".
    deny_mode: str = "tool_result"
    #: Unguessable path segment protecting the public gateway URL.
    gateway_secret: str = ""
    #: name -> {"url": "...", "headers": {...}} for the HTTP gateway.
    upstreams: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    @staticmethod
    def path() -> Path:
        return app_dir() / "config.json"

    @classmethod
    def load(cls) -> "Settings":
        path = cls.path()
        data: Dict[str, Any] = {}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise SystemExit(f"mcpguard: cannot read {path}: {exc}")
        known = {f.name for f in fields(cls)}
        settings = cls(**{k: v for k, v in data.items() if k in known})
        if not settings.gateway_secret:
            settings.gateway_secret = secrets.token_urlsafe(24)
            settings.save()
        return settings

    def save(self) -> None:
        write_private(self.path(), json.dumps(asdict(self), indent=2) + "\n")


# --------------------------------------------------------------------------
# Runtime file (daemon -> shims)
# --------------------------------------------------------------------------

def runtime_path() -> Path:
    return app_dir() / "runtime.json"


def write_runtime(port: int, token: str) -> None:
    write_private(
        runtime_path(),
        json.dumps({"pid": os.getpid(), "port": port, "token": token}),
    )


def read_runtime() -> Optional[Dict[str, Any]]:
    try:
        return json.loads(runtime_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def clear_runtime() -> None:
    info = read_runtime()
    if info and info.get("pid") == os.getpid():
        try:
            runtime_path().unlink()
        except OSError:
            pass
