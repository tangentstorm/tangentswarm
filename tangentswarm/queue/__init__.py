"""Agent-to-hub message queue: a persistent SQLite-backed inbox that replaces
ad-hoc tmux keystrokes for routine status/question/blocker/done traffic
between agents and their operator.

    tangentswarm.queue.db      Queue: the SQLite schema and operations
    tangentswarm.queue.hub     HTTP server wrapping a Queue (`swarm-hub`)
    tangentswarm.queue.client  thin HTTP client used by the hub's consumers

See tangentswarm/queue_mcp_server.py for the agent-side MCP tool
(send_message) that POSTs to the hub.
"""
