import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DEMO = str(ROOT / "examples" / "demo_server.py")
sys.path.insert(0, str(ROOT))

from mcpguard.broker import Prompter  # noqa: E402
from mcpguard.config import Settings  # noqa: E402
from mcpguard.service import GuardService  # noqa: E402


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ManualPrompter(Prompter):
    """Stands in for the popup: tests answer requests explicitly."""

    def __init__(self):
        self.requests = queue.Queue()
        self.dismissed = []

    def show(self, request, timeout):
        self.requests.put(request)

    def dismiss(self, request_id):
        self.dismissed.append(request_id)

    def next(self, timeout=10):
        return self.requests.get(timeout=timeout)

    def answer(self, request, allow):
        self._resolver(request.id, allow, "")


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPGUARD_HOME", str(tmp_path))
    monkeypatch.setenv("MCPGUARD_NO_AUTOSTART", "1")
    return tmp_path


@pytest.fixture
def settings(home):
    settings = Settings.load()
    settings.control_port, settings.gateway_port = free_port(), free_port()
    settings.approval_timeout = 15
    settings.save()
    return settings


@pytest.fixture
def guard(settings):
    prompter = ManualPrompter()
    service = GuardService(settings, prompter)
    service.start()
    yield prompter
    service.stop()


class Shim:
    """A running `mcpguard stdio` process wrapped around the demo server."""

    def __init__(self):
        env = dict(os.environ, PYTHONPATH=str(ROOT))
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "mcpguard", "stdio", "--name", "demo", "--", sys.executable, DEMO],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env)
        self.replies = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.proc.stdout:
            self.replies.put(json.loads(line))

    def send(self, msg):
        self.proc.stdin.write(json.dumps(msg).encode() + b"\n")
        self.proc.stdin.flush()

    def call(self, rid, tool, **arguments):
        self.send({"jsonrpc": "2.0", "id": rid, "method": "tools/call",
                   "params": {"name": tool, "arguments": arguments}})

    def reply(self, timeout=10):
        return self.replies.get(timeout=timeout)

    def close(self):
        self.proc.stdin.close()
        return self.proc.wait(timeout=10)


@pytest.fixture
def shim(settings):
    shim = Shim()
    yield shim
    if shim.proc.poll() is None:
        shim.proc.kill()


def wait_until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False
