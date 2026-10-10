"""Websocket client for the cloud hub. It has the interactive REPL (port of scialect
src/client.mts) and the helpers behind the cloud_* MCP tools (port of the logic in
scialect src/mcp-server.mts)."""
import asyncio
import json
import os
import sys
import time

from . import protocol as P

TRANSIENT_STATUSES = {'running', 'awaiting', 'unknown'}


class HubError(RuntimeError):
    pass


class HubClient:
    def __init__(self, url=None, timeout=60.0, on_event=None):
        self.url = url or P.default_url()
        self.timeout = timeout
        self.on_event = on_event
        self.ws = None
        self.pending = {}
        self._reader = None

    async def connect(self):
        from websockets.asyncio.client import connect
        try:
            self.ws = await connect(self.url, max_size=None, open_timeout=10)
        except Exception as e:
            raise HubError(f"ws connect failed: {self.url} ({e})") from e
        self._reader = asyncio.create_task(self._read())
        return self

    async def _read(self):
        try:
            async for raw in self.ws:
                try:
                    frame = json.loads(raw)
                except ValueError:
                    continue
                if frame.get('kind') == 'event':
                    if self.on_event:
                        self.on_event(frame)
                    continue
                fut = self.pending.pop(frame.get('id'), None)
                if fut and not fut.done():
                    fut.set_result(frame)
        finally:
            for fut in self.pending.values():
                if not fut.done():
                    fut.set_result({'id': '?', 'kind': 'error', 'message': 'ws closed'})
            self.pending.clear()

    async def call(self, body, timeout=None):
        if self.ws is None:
            await self.connect()
        rid = P.new_id()
        fut = asyncio.get_running_loop().create_future()
        self.pending[rid] = fut
        await self.ws.send(json.dumps({**body, 'id': rid}))
        try:
            return await asyncio.wait_for(fut, timeout or self.timeout)
        except asyncio.TimeoutError:
            self.pending.pop(rid, None)
            raise HubError(f"ws timeout after {timeout or self.timeout}s for {body.get('kind')}")

    async def close(self):
        if self.ws is not None:
            await self.ws.close()
        if self._reader:
            try:
                await self._reader
            except Exception:
                pass

    async def __aenter__(self):
        return await self.connect()

    async def __aexit__(self, *exc):
        await self.close()


def expect(reply, kind):
    if reply.get('kind') == kind:
        return reply
    if reply.get('kind') == 'error':
        raise HubError(f"cloud hub error: {reply.get('message')}")
    raise HubError(f"unexpected reply kind: {reply.get('kind')}")


def summarize_chat(c):
    status = f" [{c['status']}]" if c.get('status') else ''
    slug = f" ({c['slug']})" if c.get('slug') else ''
    return f"{c['id']}{status}{slug}"


# -- the four scialect MCP tools ----------------------------------------------

async def cloud_list_sessions(url=None):
    async with HubClient(url) as h:
        r = expect(await h.call({'kind': 'list'}), 'list')
    return {'active': r.get('active'), 'sessions': r['chats'],
            'summary': [summarize_chat(c) for c in r['chats']]}


async def cloud_send_message(session_id, text, url=None):
    async with HubClient(url) as h:
        expect(await h.call({'kind': 'use', 'chatId': session_id}), 'use')
        expect(await h.call({'kind': 'send', 'text': text}), 'ok')
    return {'sent_to': session_id}


async def cloud_get_latest_response(session_id, url=None):
    async with HubClient(url) as h:
        expect(await h.call({'kind': 'use', 'chatId': session_id}), 'use')
        r = expect(await h.call({'kind': 'latest'}), 'latest')
    return {'session': session_id, 'text': r.get('text')}


async def poll_until_settled(h, session_id, baseline, timeout_s, poll_s, clock=time.monotonic):
    started = clock()
    while True:
        status = expect(await h.call({'kind': 'status', 'chatId': session_id}), 'status')['chat'].get('status') or 'unknown'
        if status not in TRANSIENT_STATUSES:
            text = expect(await h.call({'kind': 'latest'}), 'latest').get('text')
            if text is not None and text != baseline:
                return {'status': status, 'text': text, 'settled': True, 'elapsed_sec': round(clock() - started, 2)}
        if clock() - started > timeout_s:
            text = expect(await h.call({'kind': 'latest'}), 'latest').get('text')
            return {'status': status, 'text': text, 'settled': False, 'elapsed_sec': round(clock() - started, 2)}
        await asyncio.sleep(poll_s)


async def cloud_wait_for_response(session_id, text=None, timeout_sec=120, poll_ms=1500, url=None):
    timeout_sec = max(1, min(int(timeout_sec), 600))
    poll_ms = max(250, min(int(poll_ms), 10_000))
    async with HubClient(url) as h:
        expect(await h.call({'kind': 'use', 'chatId': session_id}), 'use')
        baseline = expect(await h.call({'kind': 'latest'}), 'latest').get('text')
        if text is not None:
            expect(await h.call({'kind': 'send', 'text': text}), 'ok')
        return await poll_until_settled(h, session_id, baseline, timeout_sec, poll_ms / 1000.0)


# -- REPL ---------------------------------------------------------------------

HELP = """  /list                 list every chat
  /use <name>           switch active chat
  /status [name]        active chat status (or named)
  /latest               latest assistant reply in active chat
  /quit                 disconnect
  <anything else>       send as a message to active chat"""


async def repl(url=None):
    def on_event(f):
        if f.get('type') == 'hello':
            print(f"[server v{f.get('serverVersion')}]")
        elif f.get('type') == 'chat-update':
            print(f"\n[update] {f['chat']['id']} → {f['chat'].get('status', '?')}")

    h = HubClient(url, on_event=on_event)
    await h.connect()
    print(f"[tangentswarm] connected to {h.url}")
    print('commands: /list  /use <name>  /status [name]  /latest  /help  /quit')
    active = '(no chat)'

    async def use(name):
        nonlocal active
        r = await h.call({'kind': 'use', 'chatId': name})
        if r.get('kind') == 'use':
            active = r['active']['label']
            print(f"-> {active}")
        else:
            print(f"[err] {r.get('message', r)}")

    if os.environ.get('TANGENTSWARM_USE') or os.environ.get('SCIALECT_USE'):
        await use(os.environ.get('TANGENTSWARM_USE') or os.environ.get('SCIALECT_USE'))
    loop = asyncio.get_running_loop()
    try:
        while True:
            line = await loop.run_in_executor(None, lambda: input(f"{active} > "))
            line = line.strip()
            if not line:
                continue
            try:
                if not line.startswith('/'):
                    r = await h.call({'kind': 'send', 'text': line})
                    if r.get('kind') == 'error':
                        print(f"[err] {r['message']}")
                    continue
                cmd, _, arg = line[1:].partition(' ')
                arg = arg.strip()
                if cmd == 'list':
                    r = await h.call({'kind': 'list'})
                    if r.get('kind') != 'list':
                        print(f"[err] {r.get('message', r)}")
                        continue
                    if not r['chats']:
                        print('(no chats)')
                    for c in r['chats']:
                        mark = '*' if c['id'] == r.get('active') else ' '
                        print(f"{mark} [{c['transport']}/{c.get('status', '?')}] {c['label']}")
                elif cmd == 'use':
                    if not arg:
                        print('usage: /use <chat name>')
                    else:
                        await use(arg)
                elif cmd == 'status':
                    r = await h.call({'kind': 'status', 'chatId': arg} if arg else {'kind': 'status'})
                    print(f"{r['chat']['id']}: {r['chat'].get('status', '?')}" if r.get('kind') == 'status'
                          else f"[err] {r.get('message', r)}")
                elif cmd == 'latest':
                    r = await h.call({'kind': 'latest'})
                    print((r.get('text') or '(no message yet)') if r.get('kind') == 'latest'
                          else f"[err] {r.get('message', r)}")
                elif cmd == 'help':
                    print(HELP)
                elif cmd in ('quit', 'exit'):
                    break
                else:
                    print(f"unknown command: /{cmd}")
            except HubError as e:
                print(f"[err] {e}", file=sys.stderr)
    except (EOFError, KeyboardInterrupt):
        pass
    finally:
        await h.close()
        print('\n[tangentswarm] disconnected')
