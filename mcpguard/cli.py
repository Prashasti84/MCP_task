"""Command line entry point: ``python -m mcpguard <command>``."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
from pathlib import Path
from typing import List, Optional

from . import __version__
from .config import Settings, app_dir, read_runtime


def _cmd_daemon(args) -> int:
    from .service import AlreadyRunning, GuardService
    from .ui import make_prompter

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    settings = Settings.load()
    prompter = make_prompter(args.ui)
    service = GuardService(settings, prompter)
    try:
        service.start()
    except AlreadyRunning:
        print("mcpguard: a daemon is already running (ports in use).", file=sys.stderr)
        return 0
    print(f"MCP Guard {__version__} running. Control API on {settings.host}:"
          f"{settings.control_port}, gateway on {settings.host}:{settings.gateway_port}. "
          "Ctrl+C to stop.", flush=True)
    for signame in ("SIGINT", "SIGTERM", "SIGBREAK"):
        if hasattr(signal, signame):
            signal.signal(getattr(signal, signame), lambda *_: prompter.stop())
    try:
        prompter.run()  # the UI owns the main thread
    finally:
        service.stop()  # denies everything still pending
    return 0


def _cmd_stdio(argv: List[str]) -> int:
    from .client import DaemonClient
    from .stdio_proxy import StdioProxy

    if "--" not in argv:
        print("usage: mcpguard stdio --name NAME -- COMMAND [ARGS...]", file=sys.stderr)
        return 2
    split = argv.index("--")
    parser = argparse.ArgumentParser(prog="mcpguard stdio")
    parser.add_argument("--name", required=True)
    opts = parser.parse_args(argv[:split])
    command = argv[split + 1:]
    if not command:
        parser.error("missing server command after --")
    settings = Settings.load()
    code = StdioProxy(opts.name, command, settings, DaemonClient(settings)).run()
    # A reader thread may still be blocked on stdin; skip interpreter teardown,
    # which can deadlock on the stdin lock.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)


def _gateway_urls(settings: Settings) -> List[str]:
    return [f"http://{settings.host}:{settings.gateway_port}/mcp/{settings.gateway_secret}/{name}"
            for name in settings.upstreams]


def _cmd_status(_args) -> int:
    from .client import DaemonClient
    from .installer import claude_config_path

    settings = Settings.load()
    alive = DaemonClient(settings).alive()
    runtime = read_runtime() or {}
    print(f"Daemon:         {'running (pid %s)' % runtime.get('pid') if alive else 'not running'}")
    print(f"Data directory: {app_dir()}")
    print(f"Claude config:  {claude_config_path()}")
    print("Gateway routes (replace the host with your HTTPS tunnel host):")
    for url in _gateway_urls(settings) or ["  (none; add one with 'mcpguard add-upstream')"]:
        print(f"  {url}")
    return 0


def _cmd_add_upstream(args) -> int:
    settings = Settings.load()
    headers = {}
    for item in args.header or []:
        key, sep, value = item.partition(":")
        if not sep:
            raise SystemExit(f"mcpguard: bad --header '{item}', expected 'Name: value'")
        headers[key.strip()] = value.strip()
    if not args.url.startswith(("http://", "https://")):
        raise SystemExit("mcpguard: the upstream URL must start with http:// or https://")
    settings.upstreams[args.name] = {"url": args.url, "headers": headers}
    settings.save()
    print(f"Saved upstream '{args.name}'. Restart the daemon, then use this as the "
          "connector URL (with your tunnel host):")
    print(f"  /mcp/{settings.gateway_secret}/{args.name}")
    return 0


def _cmd_remove_upstream(args) -> int:
    settings = Settings.load()
    if settings.upstreams.pop(args.name, None) is None:
        print(f"No upstream named '{args.name}'.")
        return 1
    settings.save()
    print(f"Removed '{args.name}'. Restart the daemon to apply.")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["stdio"]:  # parsed by hand: everything after -- is the server
        return _cmd_stdio(argv[1:])

    from .installer import claude_config_path, install, uninstall

    parser = argparse.ArgumentParser(prog="mcpguard", description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("daemon", help="run the approval daemon (popups, control API, gateway)")
    p.add_argument("--ui", default="tk", choices=["tk", "test-allow", "test-deny"],
                   help="tk = real popups; test-* answer automatically (testing only)")
    p.set_defaults(func=_cmd_daemon)

    sub.add_parser("stdio", help="shim for one local MCP server: stdio --name NAME -- CMD ...")

    for name, func, text in (("install", install, "wrap Claude Desktop's local MCP servers"),
                             ("uninstall", uninstall, "restore Claude Desktop's config")):
        p = sub.add_parser(name, help=text)
        p.add_argument("--config", type=Path, default=None, help="path to claude_desktop_config.json")
        p.set_defaults(func=lambda a, f=func: f(a.config or claude_config_path()) or 0)

    p = sub.add_parser("add-upstream", help="register a remote MCP server behind the HTTP gateway")
    p.add_argument("name")
    p.add_argument("url")
    p.add_argument("--header", action="append", metavar="'Name: value'",
                   help="static header to send upstream (repeatable), e.g. an API key")
    p.set_defaults(func=_cmd_add_upstream)

    p = sub.add_parser("remove-upstream", help="remove a gateway upstream")
    p.add_argument("name")
    p.set_defaults(func=_cmd_remove_upstream)

    p = sub.add_parser("status", help="show daemon state, paths and gateway URLs")
    p.set_defaults(func=_cmd_status)

    args = parser.parse_args(argv)
    return int(args.func(args) or 0)
