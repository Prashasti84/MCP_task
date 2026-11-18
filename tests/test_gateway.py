"""End-to-end tests of the HTTP gateway against the demo server in HTTP mode."""

import json
import subprocess
import sys
import threading
import urllib.error
import urllib.request

import pytest

from conftest import DEMO, ManualPrompter, free_port, wait_until
from mcpguard.service import GuardService


@pytest.fixture
def gateway(settings):
    port = free_port()
    upstream = subprocess.Popen([sys.executable, DEMO, "--http", str(port)])
    settings.upstreams = {"demo": {"url": f"http://127.0.0.1:{port}/mcp", "headers": {}}}
    prompter = ManualPrompter()
    service = GuardService(settings, prompter)
    service.start()

    def up():
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/mcp", timeout=1)
        except urllib.error.HTTPError:
            return True
        except OSError:
            return False

    assert wait_until(up, 10)
    base = f"http://127.0.0.1:{settings.gateway_port}/mcp/{settings.gateway_secret}/demo"
    yield prompter, base
    service.stop()
    upstream.kill()


def post(url, msg):
    request = urllib.request.Request(url, data=json.dumps(msg).encode(), headers={
        "Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read())


def call(rid, tool, **arguments):
    return {"jsonrpc": "2.0", "id": rid, "method": "tools/call",
            "params": {"name": tool, "arguments": arguments}}


def test_passthrough(gateway):
    prompter, base = gateway
    assert len(post(base, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]) == 3
    assert prompter.requests.empty()


def test_allow_and_block(gateway):
    prompter, base = gateway
    results = {}

    def run(rid, tool, **arguments):
        results[rid] = post(base, call(rid, tool, **arguments))

    threads = [threading.Thread(target=run, args=(1, "add"), kwargs={"a": 20, "b": 22}),
               threading.Thread(target=run, args=(2, "delete_file"), kwargs={"path": "/tmp/x"})]
    for thread in threads:
        thread.start()
    for _ in threads:
        request = prompter.next()
        assert request.server == "demo" and not results  # both still pending
        prompter.pending = getattr(prompter, "pending", []) + [request]
    for request in prompter.pending:
        prompter.answer(request, request.tool == "add")
    for thread in threads:
        thread.join(15)
    assert results[1]["result"]["content"][0]["text"] == "42"
    assert results[2]["result"]["isError"] is True
    assert "Blocked by MCP Guard" in results[2]["result"]["content"][0]["text"]


def test_wrong_secret_is_rejected(gateway):
    prompter, base = gateway
    bad = base.replace("/mcp/", "/mcp/x", 1)
    with pytest.raises(urllib.error.HTTPError) as error:
        post(bad, call(1, "add", a=1, b=1))
    assert error.value.code == 404 and prompter.requests.empty()
