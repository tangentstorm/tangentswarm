"""Agent-to-hub message queue: a persistent SQLite-backed inbox that replaces
ad-hoc tmux keystrokes for routine status/question/blocker/done traffic
between agents and their operator.

    tangentswarm.queue.db      Queue: the SQLite schema and operations
    tangentswarm.queue.hub     HTTP server wrapping a Queue (`swarm-hub`)
    tangentswarm.queue.client  thin HTTP client used by the hub's consumers

See tangentswarm/agent_mcp_server.py (`swarm-agent`) for the agent-side MCP
tools (send_message, read_messages) that talk to the hub, over stdio or as one
shared HTTP server for every agent.
"""
