"""Tests for the agent-side MCP server (swarm-agent-hub): the real hub
running over HTTP, the real MCP tool dispatch (server.call_tool), one tool
only, and a bad hub URL surfacing as a ToolError rather than a crash."""
import threading

import anyio
import pytest

from tangentswarm import queue_mcp_server
from tangentswarm.queue.hub import run_hub


@pytest.fixture
def hub_url():
    ready = threading.Event()
    box = {}

    def on_ready(port):
        box['port'] = port
        ready.set()

    t = threading.Thread(target=run_hub, args=(':memory:',), kwargs={'port': 0, 'ready': on_ready},
                          daemon=True)
    t.start()
    assert ready.wait(5), 'hub did not start'
    yield f"http://127.0.0.1:{box['port']}"


def tool_names():
    return [t.name for t in anyio.run(queue_mcp_server.build_server().list_tools)]


def test_only_send_message_is_exposed():
    assert tool_names() == ['send_message']


def test_send_message_tool_reaches_the_real_hub(hub_url, monkeypatch):
    monkeypatch.setenv('TANGENTSWARM_HUB_URL', hub_url)
    server = queue_mcp_server.build_server()
    result = anyio.run(server.call_tool, 'send_message',
                        {'sender': 'agent-1', 'recipient': 'michal', 'type': 'done',
                         'body': 'tests pass', 'context': None})
    from tangentswarm.queue import client
    msgs = client.read_messages('michal', since=0, hub_url=hub_url)
    assert len(msgs) == 1
    assert msgs[0]['body'] == 'tests pass'
    assert msgs[0]['type'] == 'done'


def test_send_message_tool_error_on_unreachable_hub(monkeypatch):
    monkeypatch.setenv('TANGENTSWARM_HUB_URL', 'http://127.0.0.1:1')
    server = queue_mcp_server.build_server()
    with pytest.raises(Exception):
        anyio.run(server.call_tool, 'send_message',
                  {'sender': 'agent-1', 'recipient': 'michal', 'type': 'status', 'body': 'x'})
