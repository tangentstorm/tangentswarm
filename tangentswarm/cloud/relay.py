"""Forward requests to the cloud server that owns the browser (port of scialect src/cloud-relay.mts)."""
import asyncio
import json
import sys


class CloudRelay:
    def __init__(self, url, request_timeout=30.0, reconnect_every=5.0):
        self.url = url
        self.request_timeout = request_timeout
        self.reconnect_every = reconnect_every
        self.ws = None
        self.pending = {}
        self._task = None

    def start(self):
        self._task = asyncio.create_task(self._run())
        return self

    async def _run(self):
        from websockets.asyncio.client import connect
        while True:
            try:
                async with connect(self.url, max_size=None) as ws:
                    self.ws = ws
                    print('[cloud-relay] connected to cloud server', file=sys.stderr)
                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                        except ValueError:
                            print('[cloud-relay] bad message from cloud', file=sys.stderr)
                            continue
                        fut = self.pending.pop(msg.get('id'), None) if isinstance(msg, dict) else None
                        if fut and not fut.done():
                            fut.set_result(msg)
                print('[cloud-relay] cloud connection closed', file=sys.stderr)
            except (OSError, Exception) as e:  # noqa: BLE001  (keep retrying, like the TS version)
                if not isinstance(e, asyncio.CancelledError):
                    print(f'[cloud-relay] cloud connection error {e}', file=sys.stderr)
                else:
                    raise
            finally:
                self.ws = None
            await asyncio.sleep(self.reconnect_every)

    def is_connected(self):
        return self.ws is not None

    async def forward(self, request):
        rid = request.get('id')
        if not self.is_connected():
            return {'id': rid, 'kind': 'error', 'message': 'cloud server unavailable (try again later)'}
        fut = asyncio.get_running_loop().create_future()
        self.pending[rid] = fut
        await self.ws.send(json.dumps(request))
        try:
            return await asyncio.wait_for(fut, self.request_timeout)
        except asyncio.TimeoutError:
            self.pending.pop(rid, None)
            return {'id': rid, 'kind': 'error', 'message': 'cloud request timed out'}
