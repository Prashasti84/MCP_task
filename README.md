# MCP Guard

A Python application that intercepts Model Context Protocol (MCP) requests made by **Claude Desktop** and **Claude Web**, holds each one, and shows an always-on-top popup where the user chooses **Allow** or **Block**. Nothing reaches the MCP server until the user allows it.

- Every `tools/call` is held until a decision is made. Allowed calls are forwarded unchanged; blocked calls never reach the server and Claude receives a clear denial.
- Any number of requests can be pending at once. Each gets its own popup and its own decision.
- Fail closed: timeout, closed popup, crashed daemon, lost connection and shutdown all result in a block.

## Contents

1. [How Claude talks to MCP servers](#how-claude-talks-to-mcp-servers)
2. [Architecture](#architecture)
3. [Install](#install)
4. [Use with Claude Desktop](#use-with-claude-desktop)
5. [Use with Claude Web](#use-with-claude-web)
6. [Demo script](#demo-script)
7. [Configuration](#configuration)
8. [Reliability and edge cases](#reliability-and-edge-cases)
9. [Security](#security)
10. [Limitations](#limitations)
11. [Tests](#tests)
12. [Project layout](#project-layout)

## How Claude talks to MCP servers

The interception design follows from where the traffic actually flows. There are two separate mechanisms:

| | Local MCP servers | Remote MCP servers (custom connectors) |
|---|---|---|
| Used by | Claude Desktop only | Claude Web (claude.ai), Claude Desktop, mobile |
| Configured in | `claude_desktop_config.json` | Connector settings in the Claude account |
| Transport | stdio: newline-delimited JSON-RPC over the child process's stdin/stdout | Streamable HTTP (or SSE) over HTTPS |
| Who opens the connection | The Desktop app, on your machine | **Anthropic's cloud**, on every client |

The last row matters most. For remote connectors the MCP request never passes through the browser or the user's network: Anthropic's servers call the MCP server directly ([Anthropic's connector documentation](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp) states this explicitly). A packet sniffer, browser extension or local TLS proxy on the user's machine therefore cannot see Claude Web's MCP traffic at all. The only place a user-controlled program can intercept it is **at the connector URL itself**.

So MCP Guard intercepts at the one point each transport offers:

- **stdio**: it becomes the process Claude Desktop launches, and relays to the real server.
- **HTTP**: it becomes the URL Claude connects to, and relays to the real server.

Both are protocol-level proxies, so they see the exact server, tool name and parameters, and they can hold or refuse a request cleanly. No TLS interception, OS hooks or elevated privileges are needed.

## Architecture

```
 Claude Desktop                                     Claude Web / remote connectors
      |  stdio (JSON-RPC)                                    |
      v                                                 Anthropic cloud
 +-----------+   stdio    +-----------------+                | HTTPS
 | stdio shim| ---------> | real local MCP  |                v
 | (one per  | <--------- | server          |         HTTPS tunnel (cloudflared, ngrok)
 |  server)  |            +-----------------+                |
 +-----------+                                               v
      | "may I?" (loopback HTTP + bearer token)   +----------------------+   HTTP(S)   +-------------+
      +-----------------------------------------> |      DAEMON          | ----------> | real remote |
                                                  |  control API  :8765  | <---------- | MCP server  |
                                                  |  HTTP gateway :8766  |             +-------------+
                                                  |  approval broker     |
                                                  |  Tk popups (topmost) |
                                                  +----------------------+
                                                       |
                                                       v
                                               audit.jsonl (decisions)
```

Components (one module each in `mcpguard/`):

| Module | Role |
|---|---|
| `stdio_proxy.py` | The shim. Relays stdio traffic byte for byte. Parks each guarded request on its own thread while it asks the daemon, so the relay itself never blocks. Standard library only. |
| `gateway.py` | Streamable HTTP reverse proxy. Forwards POST, GET (SSE) and DELETE to the upstream, streaming responses through. Guarded POSTs wait on the broker first. |
| `broker.py` | The core. One `asyncio.Future` per pending request, keyed by a unique id. Resolves on Allow, Block, timeout, client cancellation, requester disconnect or shutdown. Writes the audit log. |
| `ui.py` | Tkinter popups. One topmost window per pending request showing source, server, method, tool, tool description and the full parameters. |
| `service.py` | Runs the control API and the gateway on a background asyncio thread; Tk keeps the main thread. Binding the port doubles as the single-instance lock. |
| `client.py` | The shim's client for the control API. Auto-starts the daemon when needed. |
| `installer.py` | Wraps and unwraps the servers in `claude_desktop_config.json`, with backups. |
| `protocol.py` | JSON-RPC helpers and the denial responses. |
| `config.py` | Settings and private file handling. |

### Life of a request

1. Claude sends `{"method": "tools/call", "params": {"name": "delete_file", "arguments": {...}}}`.
2. The interceptor (shim or gateway) recognises a guarded method and does **not** forward it. It submits an approval request to the broker and waits.
3. The broker creates a future and asks the UI to open a popup.
4. The user clicks:
   - **Allow**: the original bytes are forwarded to the real server and its response flows back as usual.
   - **Block**: nothing is forwarded. Claude receives a response for that request id saying it was blocked.
5. The decision is appended to `audit.jsonl`.

Everything else (`initialize`, `tools/list`, notifications, responses) passes straight through, so guarded calls in flight never stall the session.

### What Claude receives when a call is blocked

For `tools/call` the default is a normal tool result with `isError: true` and the text `Blocked by MCP Guard: blocked by user`. MCP defines `isError` results as the way to report tool failures to the model, so Claude can tell the user what happened instead of showing a transport failure. Set `"deny_mode": "jsonrpc_error"` to return a JSON-RPC error (code `-32001`) instead.

## Install

Requirements: Python 3.9+ with Tkinter (bundled with the python.org installers for Windows and macOS; on Debian/Ubuntu run `sudo apt install python3-tk`; with Homebrew run `brew install python-tk`).

```bash
git clone <your-repo-url> mcp-guard
cd mcp-guard
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e .
```

Check it works:

```bash
mcpguard status
```

## Use with Claude Desktop

1. Make sure Claude Desktop has at least one local MCP server. To try the bundled demo server, open **Settings > Developer > Edit Config** and add (use absolute paths):

   ```json
   {
     "mcpServers": {
       "demo": {
         "command": "/absolute/path/to/mcp-guard/.venv/bin/python",
         "args": ["/absolute/path/to/mcp-guard/examples/demo_server.py"]
       }
     }
   }
   ```

2. Wrap the servers (run inside the virtual environment so the shim uses the right Python):

   ```bash
   mcpguard install
   ```

   This rewrites each entry to launch the shim and saves a timestamped backup next to the config file. It is idempotent. `mcpguard uninstall` restores the original commands.

3. Start the daemon. It runs in the background of your session and has no main window:

   ```bash
   mcpguard daemon
   ```

   If you skip this step, the first shim that Claude Desktop launches starts the daemon for you (output goes to `~/.mcpguard/daemon.log`).

4. Fully quit and reopen Claude Desktop, then ask Claude to use a tool. The popup appears above all windows.

## Use with Claude Web

Claude Web reaches MCP servers from Anthropic's cloud, so the gateway must be reachable over public HTTPS on port 443. A tunnel does that.

1. Register the real MCP server as an upstream:

   ```bash
   mcpguard add-upstream demo http://127.0.0.1:9000/mcp
   # with a static API key:
   # mcpguard add-upstream notes https://mcp.example.com/mcp --header "Authorization: Bearer <key>"
   ```

   The command prints the gateway path: `/mcp/<secret>/demo`.

2. For the demo, start the bundled server in HTTP mode:

   ```bash
   python examples/demo_server.py --http 9000
   ```

3. Start (or restart) the daemon, then open a tunnel to the **gateway port only**:

   ```bash
   mcpguard daemon
   cloudflared tunnel --url http://127.0.0.1:8766     # or: ngrok http 8766
   ```

4. In Claude (web or desktop): **Settings > Connectors > Add custom connector** and enter

   ```
   https://<your-tunnel-host>/mcp/<secret>/demo
   ```

5. Ask Claude to use one of the connector's tools. The popup appears on your machine, and the request from Anthropic's cloud stays open until you decide.

Because the connector belongs to your Claude account, the same gateway also guards that connector when it is used from Claude Desktop or mobile.

## Demo script

Suggested flow for the demo video (about three minutes):

1. Show `mcpguard status`, the wrapped `claude_desktop_config.json` and `mcpguard daemon` running.
2. **Approve**: in Claude Desktop ask "Use the add tool to add 20 and 22". Show the popup (server, tool, parameters), click **Allow**, show the answer 42.
3. **Block**: ask "Use delete_file to delete /tmp/important.txt". Click **Block**. Claude reports that the call was blocked.
4. **Concurrency**: ask "Call echo three times in parallel with the texts one, two and three". Show three popups, allow two and block one.
5. **Timeout**: trigger a call and leave the popup alone with a short `approval_timeout`.
6. **Claude Web**: repeat steps 2 and 3 on claude.ai through the gateway connector.
7. Show `~/.mcpguard/audit.jsonl`.

## Configuration

`~/.mcpguard/config.json` (created on first run; set `MCPGUARD_HOME` to move the directory):

| Key | Default | Meaning |
|---|---|---|
| `control_port` | `8765` | Loopback control API used by shims. Never expose it. |
| `gateway_port` | `8766` | HTTP gateway for remote connectors. This is the port you tunnel. |
| `approval_timeout` | `120` | Seconds before an unanswered request is blocked. |
| `max_pending` | `25` | Maximum simultaneous popups; further requests are blocked immediately. |
| `guarded_methods` | `["tools/call"]` | JSON-RPC methods that need approval. Add `resources/read` or `prompts/get` for stricter control. |
| `deny_mode` | `tool_result` | `tool_result` or `jsonrpc_error`. |
| `gateway_secret` | random | Secret path segment of the gateway URL. |
| `upstreams` | `{}` | Managed by `add-upstream` / `remove-upstream`. |

Commands: `daemon`, `install`, `uninstall`, `add-upstream`, `remove-upstream`, `status`, and `stdio` (used by Claude Desktop, not by hand).

## Reliability and edge cases

| Situation | Behaviour |
|---|---|
| Several requests at once | Independent futures and popups; answers may come in any order. Other traffic keeps flowing. |
| No answer | Blocked after `approval_timeout`; the popup closes. |
| Popup closed with X or Esc | Blocked. |
| Claude cancels a pending request (`notifications/cancelled`) | Popup closes; the server never sees the request. |
| Claude Desktop quits or closes a server | The shim cancels its pending popups, closes the server's stdin, then terminates it if it does not exit. |
| MCP server crashes | The shim exits with the server's exit code, so Claude Desktop sees the failure as it would without the guard. |
| Remote client disconnects while pending | The waiting handler is cancelled and the popup closes. |
| Daemon not running | The shim tries to start it; if that fails the call is blocked with an explanatory message. |
| Daemon stopped (Ctrl+C, SIGTERM) | Every pending request is blocked before exit; `runtime.json` is removed. |
| Daemon started twice | The second instance sees the port in use and exits. |
| Popup flood | Capped by `max_pending`. |
| Malformed or non-JSON lines | Relayed untouched; a bad message cannot stop the relay. |
| Guarded method sent as a notification (no id) | Dropped, since it could not be answered or blocked visibly. |
| Upstream HTTP server down | Gateway returns `502` with a JSON-RPC error. |

## Security

- **Fail closed everywhere.** Only an explicit Allow click forwards a request.
- **Control API**: bound to `127.0.0.1`, and every call needs a random bearer token generated at daemon start and stored in `runtime.json` with `0600` permissions. Other local users and web pages cannot forge approval traffic. Tokens are compared in constant time.
- **Gateway**: the public URL contains a 192-bit random secret, so someone who discovers the tunnel host cannot reach the upstream or trigger popups. Only the gateway port is tunnelled; the control API is on a different port.
- **Popup safety**: parameters are rendered as plain text. The default focus is on **Block**, and **Allow** is disabled for the first 0.7 s, so a keystroke or click meant for another window cannot approve a popup that just appeared.
- **Audit log** stores parameter names, not values, to avoid copying secrets to disk.
- **Least privilege**: no admin rights, no certificate installation, no traffic decryption. The shim adds no new access; it runs the same command Claude Desktop would have run.

## Limitations

These follow from how Claude routes MCP traffic, and are stated plainly:

- **Connectors that Claude reaches directly cannot be intercepted by any local application.** That covers Anthropic's built-in directory connectors and any custom connector whose URL does not point at the gateway. The traffic goes from Anthropic's cloud to the provider. To guard a remote server, add it to Claude through the gateway URL.
- **OAuth-protected upstreams** are not supported by the gateway yet. It supports upstreams that need no auth, a static header (API key), or a bearer token that Claude itself sends. Proxying the OAuth discovery and token endpoints is the natural next step.
- **Claude Desktop extensions** installed from the in-app extension directory are managed outside `mcpServers` and are not wrapped by `mcpguard install`. Servers in `claude_desktop_config.json` are.
- **Gateway batches** are all or nothing: if one guarded call in a JSON-RPC batch is blocked, the whole POST is refused. (Batching was removed from MCP in the 2025-06-18 revision.)
- The gateway popup does not show the tool description (the stdio popup does, learned from `tools/list`).
- A very long wait on the HTTP path depends on the remote client's own timeout; keep `approval_timeout` modest.

## Tests

```bash
pip install -e ".[dev]"
pytest
```

The suite runs real end-to-end paths (client, shim, demo server; HTTP client, gateway, demo server) with a scripted stand-in for the popup. It covers pass-through, allow, block, five interleaved concurrent calls, timeout, cancellation, shim exit, daemon shutdown, daemon absence, token enforcement, the gateway secret and the installer.

A headless smoke run without popups is available with `mcpguard daemon --ui test-allow` or `--ui test-deny` (testing only).

## Project layout

```
mcpguard/
  cli.py          command line
  service.py      daemon: control API + gateway on an asyncio thread
  broker.py       pending requests, decisions, audit log
  ui.py           Tk popups
  stdio_proxy.py  Claude Desktop shim
  gateway.py      Claude Web HTTP gateway
  client.py       shim -> daemon client
  installer.py    claude_desktop_config.json wrapping
  protocol.py     JSON-RPC helpers
  config.py       settings and files
examples/
  demo_server.py  minimal MCP server (stdio and HTTP)
tests/
```
