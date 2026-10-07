"""Claude Code cloud sessions (claude.ai/code) driven through a persistent
Playwright browser, plus the websocket hub that brokers clients to it.

Python port of scialect's cloud transport (browser.mts, sessions.mts,
protocol.mts, handlers.mts, server.mts, cloud-relay.mts, client.mts, cli.mts
and the two vite.*.config.mts server plugins).

Needs the optional extra:  pip install 'tangentswarm[cloud]'
and a browser:             playwright install chromium
"""
