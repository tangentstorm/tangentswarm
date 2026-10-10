"""Small orchestrator on port 5002 (port of the plugin in scialect's vite.config.mts).

It answers `subscribe swarm` (with a full snapshot) and `swarm-status` itself. It
polls the local swarm (workers.jsonl in the control dir) every 2s and pushes the
changes. It relays every other request to the cloud server that owns the browser on
port 5003 (`swarm cloud serve --port 5003`).
"""
import asyncio
import json
import sys

from . import protocol as P
from .hub import Hub, path_ok
from .relay import CloudRelay
from .. import swarm_state


async def serve_orchestrator(host='127.0.0.1', port=P.DEFAULT_PORT,
                             cloud_url=f"ws://127.0.0.1:{P.CLOUD_PORT}{P.WS_PATH}",
                             cdir=None, poll=2.0, display=True, ready=None):
    from websockets.asyncio.server import serve

    hub = Hub(handle=None, open_session=lambda *a: None)
    relay = CloudRelay(cloud_url).start()

    async def handler(ws):
        if not path_ok(ws):
            await ws.close(1008, 'use /ws')
            return
        from .handlers import ClientState
        c = ClientState()
        hub.clients[ws] = c
        await hub.send(ws, P.hello())
        try:
            async for raw in ws:
                try:
                    req = json.loads(raw)
                except ValueError:
                    await hub.send(ws, P.error('?', 'bad json'))
                    continue
                if req.get('kind') == 'subscribe' and req.get('channel') == 'swarm':
                    c.subscriptions.add('swarm')
                    await hub.send(ws, {'id': req.get('id'), 'kind': 'ok'})
                    full = swarm_state.get_current_swarm_state()
                    if full:
                        await hub.send(ws, {'kind': 'event', 'type': 'swarm-status', 'changes': full})
                    continue
                if req.get('kind') == 'swarm-status':
                    await hub.send(ws, {'id': req.get('id'), 'kind': 'swarm-status',
                                        'changes': swarm_state.get_current_swarm_state()})
                    continue
                await hub.send(ws, await relay.forward(req))
        finally:
            hub.clients.pop(ws, None)

    async def poller():
        while True:
            try:
                changes = await asyncio.to_thread(swarm_state.poll_swarm_once, cdir, None, display)
                await hub.emit_swarm_status(changes)
            except Exception as e:  # noqa: BLE001
                print(f'[swarm] poll failed: {e}', file=sys.stderr)
            await asyncio.sleep(poll)

    async with serve(handler, host, port, max_size=None) as server:
        actual = server.sockets[0].getsockname()[1] if server.sockets else port
        print(f"[tangentswarm] ws://{host}:{actual}{P.WS_PATH} ready (thin, relaying cloud to {cloud_url})",
              file=sys.stderr, flush=True)
        if ready is not None:
            ready.set_result(actual)
        task = asyncio.create_task(poller()) if poll else None
        try:
            await asyncio.Future()
        finally:
            if task:
                task.cancel()
