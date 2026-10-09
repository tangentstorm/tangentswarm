"""swarm-agent-hub: a thin MCP server that runs inside each agent's own
environment (container, sandbox, whatever -- it has no tmux access and
doesn't need any) and exposes exactly one tool: send_message. It POSTs to
the hub (tangentswarm.queue.hub, normally `swarm-hub` on localhost) and
nothing else -- no tmux, no filesystem, no shell.

    swarm-agent-hub                      # stdio, hub at $TANGENTSWARM_HUB_URL
                                          # (default http://127.0.0.1:8777)

This is deliberately separate from swarm-mcp (tangentswarm/mcp_server.py),
which runs on the host and drives tmux: that one controls agents, this one
lets an agent talk back.
"""
import argparse
import sys

from . import __version__
from .queue import client

from mcp.server.mcpserver import MCPServer as _Server
from mcp.server.mcpserver.exceptions import ToolError

INSTRUCTIONS = """Send a message to the hub's message queue so a human or
another agent can see it without watching your terminal. recipient: who
should see it (a person or agent name). type: status, blocker, question, or
done. context: optional free text (repo/branch, etc)."""


def send_message(sender: str, recipient: str, type: str, body: str,
                  context: str | None = None) -> dict:
    """Send a message to the hub. type must be status, blocker, question, or done."""
    try:
        return client.send_message(sender, recipient, type, body, context)
    except client.HubError as e:
        raise ToolError(str(e)) from e


TOOLS = [send_message]


def build_server(**kwargs):
    kwargs.setdefault('version', __version__)
    server = _Server(name='tangentswarm-agent-hub', instructions=INSTRUCTIONS, **kwargs)
    for fn in TOOLS:
        server.add_tool(fn, name=fn.__name__)
    return server


def main(argv=None):
    ap = argparse.ArgumentParser(prog='swarm-agent-hub',
                                  description='Agent-side MCP client for the tangentswarm hub queue')
    ap.add_argument('--version', action='version', version=f'tangentswarm {__version__}')
    ap.parse_args(argv)
    logging_to_stderr()
    build_server().run('stdio')


def logging_to_stderr():
    import logging
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)


if __name__ == '__main__':
    main()
