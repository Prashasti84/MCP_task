"""Small helpers for the JSON-RPC 2.0 messages that MCP is built on."""

from __future__ import annotations

from typing import Any, Dict, Iterable, Tuple

#: Implementation-defined JSON-RPC error code used for user denials.
DENIED_CODE = -32001


def is_guarded(msg: Any, guarded_methods: Iterable[str]) -> bool:
    return isinstance(msg, dict) and msg.get("method") in guarded_methods


def summarize(msg: Dict[str, Any]) -> Tuple[str, Any]:
    """Return ``(tool_name, arguments)`` for display and audit."""
    params = msg.get("params")
    if msg.get("method") == "tools/call" and isinstance(params, dict):
        return str(params.get("name", "<unnamed tool>")), params.get("arguments", {})
    return str(msg.get("method")), params if params is not None else {}


def denial(msg: Dict[str, Any], reason: str, mode: str = "tool_result") -> Dict[str, Any]:
    """Build the response sent back to Claude for a blocked request.

    For ``tools/call`` the default is a *successful* JSON-RPC response whose
    result has ``isError: true``. MCP defines this as the way to report tool
    failures to the model, so Claude can tell the user the call was blocked
    instead of showing a generic transport error. Other methods (or
    ``mode="jsonrpc_error"``) get a protocol level error.
    """
    text = f"Blocked by MCP Guard: {reason}"
    if msg.get("method") == "tools/call" and mode == "tool_result":
        return {
            "jsonrpc": "2.0",
            "id": msg.get("id"),
            "result": {"content": [{"type": "text", "text": text}], "isError": True},
        }
    return {
        "jsonrpc": "2.0",
        "id": msg.get("id"),
        "error": {"code": DENIED_CODE, "message": text},
    }
