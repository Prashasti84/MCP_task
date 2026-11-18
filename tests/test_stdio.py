"""End-to-end tests of the stdio path: client -> shim -> demo MCP server."""

import queue

import pytest

from conftest import wait_until


def text(reply):
    return reply["result"]["content"][0]["text"]


def test_unguarded_traffic_passes_without_prompt(guard, shim):
    shim.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert shim.reply()["result"]["serverInfo"]["name"] == "mcpguard-demo"
    shim.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert len(shim.reply()["result"]["tools"]) == 3
    assert guard.requests.empty()


def test_allowed_call_is_held_then_executed(guard, shim):
    shim.send({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    shim.reply()
    shim.call(7, "add", a=2, b=3)
    request = guard.next()
    assert (request.server, request.tool, request.arguments) == ("demo", "add", {"a": 2, "b": 3})
    assert request.description == "Add two numbers."  # learned from tools/list
    with pytest.raises(queue.Empty):  # pending: nothing reaches the server yet
        shim.reply(timeout=0.5)
    guard.answer(request, True)
    reply = shim.reply()
    assert reply["id"] == 7 and text(reply) == "5" and reply["result"]["isError"] is False


def test_blocked_call_never_reaches_server(guard, shim):
    shim.call("x", "delete_file", path="/etc/passwd")
    guard.answer(guard.next(), False)
    reply = shim.reply()
    assert reply["id"] == "x" and reply["result"]["isError"] is True
    assert "Blocked by MCP Guard" in text(reply) and "simulated" not in text(reply)


def test_concurrent_calls_are_independent(guard, shim):
    for i in range(5):
        shim.call(i, "echo", text=f"msg{i}")
    requests = {r.arguments["text"]: r for r in (guard.next() for _ in range(5))}
    assert len(requests) == 5
    # While five calls wait, ordinary traffic still flows.
    shim.send({"jsonrpc": "2.0", "id": "ping", "method": "ping"})
    assert shim.reply()["id"] == "ping"
    # Answer out of order, mixing allow and block.
    for i in (3, 0, 4, 1, 2):
        guard.answer(requests[f"msg{i}"], i % 2 == 0)
        reply = shim.reply()
        assert reply["id"] == i
        assert reply["result"]["isError"] is (i % 2 == 1)
        if i % 2 == 0:
            assert text(reply) == f"msg{i}"


def test_timeout_blocks(home, settings, shim):
    from conftest import ManualPrompter
    from mcpguard.service import GuardService

    settings.approval_timeout = 1
    service = GuardService(settings, ManualPrompter())
    service.start()
    try:
        shim.call(1, "add", a=1, b=1)
        reply = shim.reply()
        assert reply["result"]["isError"] is True and "no answer" in text(reply)
    finally:
        service.stop()


def test_daemon_down_fails_closed(settings, shim):
    shim.call(1, "add", a=1, b=1)
    reply = shim.reply()
    assert reply["result"]["isError"] is True and "daemon is not running" in text(reply)


def test_client_cancellation_closes_prompt(guard, shim):
    shim.call(9, "add", a=1, b=1)
    request = guard.next()
    shim.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 9}})
    assert wait_until(lambda: request.id in guard.dismissed)
    with pytest.raises(queue.Empty):  # cancelled requests get no response
        shim.reply(timeout=0.5)


def test_shim_exit_closes_prompts_and_stops_server(guard, shim):
    shim.call(1, "add", a=1, b=1)
    request = guard.next()
    assert shim.close() == 0
    assert wait_until(lambda: request.id in guard.dismissed)


def test_daemon_shutdown_denies_pending(settings, shim):
    from conftest import ManualPrompter
    from mcpguard.service import GuardService

    prompter = ManualPrompter()
    service = GuardService(settings, prompter)
    service.start()
    shim.call(1, "add", a=1, b=1)
    prompter.next()
    service.stop()
    assert shim.reply()["result"]["isError"] is True


def test_control_api_requires_token(guard, settings):
    import http.client
    import json

    conn = http.client.HTTPConnection("127.0.0.1", settings.control_port, timeout=5)
    conn.request("POST", "/v1/approve", body=json.dumps({"tool": "x"}),
                 headers={"Authorization": "Bearer wrong", "Content-Type": "application/json"})
    assert conn.getresponse().status == 401
    assert guard.requests.empty()
