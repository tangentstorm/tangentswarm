"""Websocket hub that owns the Playwright page (port of scialect src/server.mts
and the vite.cloud.config.mts plugin).

    swarm cloud serve                 # browser + hub on ws://127.0.0.1:5002/ws
    swarm cloud serve --port 5003     # the "cloud" server behind the orchestrator

Binds 127.0.0.1 only; there is no authentication on this socket (same as
scialect), so never expose it.
"""
import asyncio
import json
import signal
import sys

from . import protocol as P
from .handlers import ClientState, dispatch


def log(msg):
    print(msg, file=sys.stderr, flush=True)


class Hub:
    def __init__(self, handle=None, dispatch_fn=dispatch, open_session=None):
        self.handle = handle
        self.dispatch_fn = dispatch_fn
        self.clients = {}            # ws -> ClientState
        self.browser_workers = set()
        self._page_lock = asyncio.Lock()
        self._currently_open = None
        if open_session is None:
            from .sessions import open_session as _open
            open_session = _open
        self._open_session = open_session

    # deps passed to dispatch
    @property
    def page(self):
        if self.handle is None:
            raise RuntimeError('Hub has no browser handle (browserless orchestrator mode)')
        return self.handle.page

    async def with_active_chat(self, chat_id, fn):
        """Serialize page use; navigate to chat_id first if needed."""
        async with self._page_lock:
            if self._currently_open != chat_id:
                await self._open_session(self.page, chat_id)
                self._currently_open = chat_id
            return await fn()

    async def send(self, ws, frame):
        try:
            await ws.send(P.dumps(frame))
        except Exception:
            pass

    async def broadcast(self, frame):
        for ws in list(self.clients):
            await self.send(ws, frame)

    async def emit_swarm_status(self, changes):
        if not changes:
            return
        frame = {'kind': 'event', 'type': 'swarm-status', 'changes': changes}
        for ws, c in list(self.clients.items()):
            if 'swarm' in c.subscriptions:
                await self.send(ws, frame)

    async def handle_message(self, ws, c, raw):
        try:
            req = json.loads(raw)
            if not isinstance(req, dict):
                raise ValueError('frame is not an object')
        except ValueError as e:
            await self.send(ws, P.error('?', f'bad json: {e}'))
            return
        if req.get('kind') == 'register' and req.get('workerType') == 'cloud-browser':
            self.browser_workers.add(ws)
            log('[Hub] Registered new cloud-browser worker')
            await self.send(ws, {'id': req.get('id'), 'kind': 'ok'})
            return
        try:
            reply = await self.dispatch_fn(self, c, req)
        except Exception as e:
            reply = P.error(req.get('id', '?'), str(e))
        await self.send(ws, reply)

    async def attach(self, ws):
        c = ClientState()
        self.clients[ws] = c
        await self.send(ws, P.hello())
        try:
            async for raw in ws:
                await self.handle_message(ws, c, raw)
        finally:
            self.clients.pop(ws, None)
            self.browser_workers.discard(ws)


def path_ok(ws, path=P.WS_PATH):
    try:
        req_path = ws.request.path
    except AttributeError:
        return True
    return req_path.split('?')[0].endswith(path)


async def serve_hub(hub, host='127.0.0.1', port=P.DEFAULT_PORT, ready=None):
    from websockets.asyncio.server import serve

    async def handler(ws):
        if not path_ok(ws):
            await ws.close(1008, 'use /ws')
            return
        await hub.attach(ws)

    async with serve(handler, host, port, max_size=None) as server:
        actual = server.sockets[0].getsockname()[1] if server.sockets else port
        log(f"[tangentswarm-cloud] ws://{host}:{actual}{P.WS_PATH} ready")
        if ready is not None:
            ready.set_result(actual)
        await asyncio.Future()


async def start_browser(headed=True, profile_dir=None):
    from .browser import NotLoggedIn, goto_claude_code, launch_browser
    log('[tangentswarm-cloud] launching browser…')
    handle = await launch_browser(profile_dir, headed=headed)
    try:
        await goto_claude_code(handle.page)
        log('[tangentswarm-cloud] claude.ai/code loaded.')
    except NotLoggedIn as e:
        log(f'[tangentswarm-cloud] not logged in: {e}')
    except Exception as e:
        log(f'[tangentswarm-cloud] could not load claude.ai/code: {e}')
    return handle


def run_server(host='127.0.0.1', port=P.DEFAULT_PORT, headed=True, profile_dir=None):
    async def main():
        handle = await start_browser(headed=headed, profile_dir=profile_dir)
        hub = Hub(handle)
        loop = asyncio.get_running_loop()
        stop = loop.create_future()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: stop.done() or stop.set_result(None))
        task = asyncio.create_task(serve_hub(hub, host, port))
        await stop
        log('\n[tangentswarm-cloud] shutting down…')
        task.cancel()
        await handle.close()     # clean close so the login cookie is flushed

    asyncio.run(main())
