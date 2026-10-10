"""Drive Claude Code cloud sessions (claude.ai/code) through a persistent Playwright
browser, with a websocket hub that connects clients to it.

This is a Python port of scialect's cloud transport (browser.mts, sessions.mts,
protocol.mts, handlers.mts, server.mts, cloud-relay.mts, client.mts, cli.mts
and the two vite.*.config.mts server plugins).

It needs the optional extra and a browser:

    pip install 'tangentswarm[cloud]'
    playwright install chromium
"""
