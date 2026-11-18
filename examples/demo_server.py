"""A tiny MCP server for demos and tests. No third-party SDK needed.

    python examples/demo_server.py            # stdio transport
    python examples/demo_server.py --http 9000  # Streamable HTTP on /mcp

Tools: ``add`` (harmless), ``echo`` and ``delete_file`` (pretends to delete;
it touches nothing, it just gives you something worth blocking).
"""

import json
import sys
import time

TOOLS = [
    {"name": "add", "description": "Add two numbers.",
     "inputSchema": {"type": "object", "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
                     "required": ["a", "b"]}},
    {"name": "echo", "description": "Return the given text, optionally after a delay.",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"},
                                                      "delay": {"type": "number"}},
                     "required": ["text"]}},
    {"name": "delete_file", "description": "Delete a file from disk (simulated, nothing is deleted).",
     "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}},
                     "required": ["path"]}},
]


def call_tool(name, args):
    if name == "add":
        return str(args["a"] + args["b"])
    if name == "echo":
        time.sleep(float(args.get("delay", 0)))
        return str(args["text"])
    if name == "delete_file":
        return f"(simulated) deleted {args['path']}"
    raise KeyError(name)


def handle(msg):
    """Return the response for one JSON-RPC message, or None for notifications."""
    if "id" not in msg:
        return None
    method, rid = msg.get("method"), msg["id"]
    if method == "initialize":
        result = {"protocolVersion": msg.get("params", {}).get("protocolVersion", "2025-06-18"),
                  "capabilities": {"tools": {}},
                  "serverInfo": {"name": "mcpguard-demo", "version": "1.0.0"}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        params = msg.get("params", {})
        try:
            text, is_error = call_tool(params.get("name"), params.get("arguments") or {}), False
        except Exception as exc:
            text, is_error = f"tool error: {exc!r}", True
        result = {"content": [{"type": "text", "text": text}], "isError": is_error}
    else:
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "Method not found"}}
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def serve_stdio():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        reply = handle(json.loads(line))
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()


def serve_http(port):
    import asyncio

    from aiohttp import web

    async def mcp(request):
        if request.method != "POST":
            return web.Response(status=405, headers={"Allow": "POST"})
        msg = await request.json()
        reply = await asyncio.to_thread(handle, msg)
        if reply is None:
            return web.Response(status=202)
        return web.json_response(reply)

    app = web.Application()
    app.router.add_route("*", "/mcp", mcp)
    web.run_app(app, host="127.0.0.1", port=port, print=None)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--http":
        serve_http(int(sys.argv[2]))
    else:
        serve_stdio()
