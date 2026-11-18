"""Wrap / unwrap the local MCP servers in Claude Desktop's config file.

``install`` rewrites every ``mcpServers`` entry so that Claude Desktop starts
the MCP Guard shim, which in turn starts the original command::

    {"command": "npx", "args": ["-y", "some-server"]}
        becomes
    {"command": "/path/to/python",
     "args": ["-m", "mcpguard", "stdio", "--name", "<name>", "--",
              "npx", "-y", "some-server"]}

``env`` and all other keys are left untouched. The operation is idempotent,
reversible (``uninstall``) and always writes a timestamped backup first.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

MARKER = ["-m", "mcpguard", "stdio"]


def claude_config_path() -> Path:
    override = os.environ.get("CLAUDE_DESKTOP_CONFIG")
    if override:
        return Path(override)
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "Claude"
    elif os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "Claude"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "Claude"
    return base / "claude_desktop_config.json"


def is_wrapped(entry: Dict[str, Any]) -> bool:
    args = entry.get("args") or []
    return args[:3] == MARKER and "--" in args


def wrap(config: Dict[str, Any], python: str) -> Tuple[Dict[str, Any], List[str], List[str]]:
    """Return ``(new_config, wrapped_names, skipped_names)``."""
    config = copy.deepcopy(config)
    wrapped, skipped = [], []
    for name, entry in (config.get("mcpServers") or {}).items():
        if not isinstance(entry, dict) or not entry.get("command"):
            skipped.append(name)  # e.g. URL based entries: use the HTTP gateway
            continue
        if is_wrapped(entry):
            continue
        entry["args"] = [*MARKER, "--name", name, "--", entry["command"], *(entry.get("args") or [])]
        entry["command"] = python
        wrapped.append(name)
    return config, wrapped, skipped


def unwrap(config: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    config = copy.deepcopy(config)
    restored = []
    for name, entry in (config.get("mcpServers") or {}).items():
        if isinstance(entry, dict) and is_wrapped(entry):
            original = entry["args"][entry["args"].index("--") + 1:]
            entry["command"], entry["args"] = original[0], original[1:]
            restored.append(name)
    return config, restored


def _load(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise SystemExit(f"mcpguard: Claude Desktop config not found at {path}\n"
                         "Open Claude Desktop > Settings > Developer > Edit Config once to create it.")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise SystemExit(f"mcpguard: {path} is not valid JSON: {exc}")


def _save(path: Path, config: Dict[str, Any]) -> Path:
    backup = path.with_name(f"{path.name}.mcpguard-backup-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(path, backup)
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return backup


def install(path: Path) -> None:
    config, wrapped, skipped = wrap(_load(path), sys.executable)
    if wrapped:
        backup = _save(path, config)
        print(f"Wrapped {len(wrapped)} server(s): {', '.join(wrapped)}")
        print(f"Backup: {backup}")
        print("Fully quit and reopen Claude Desktop to apply.")
    else:
        print("Nothing to wrap (no local servers, or all are already wrapped).")
    for name in skipped:
        print(f"Skipped '{name}': not a local command based server.")


def uninstall(path: Path) -> None:
    config, restored = unwrap(_load(path))
    if restored:
        backup = _save(path, config)
        print(f"Restored {len(restored)} server(s): {', '.join(restored)}")
        print(f"Backup: {backup}")
        print("Fully quit and reopen Claude Desktop to apply.")
    else:
        print("No wrapped servers found.")
