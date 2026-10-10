"""Cloud transport tests with a fake Playwright page (no browser, no network)."""
import asyncio

import pytest

pytest.importorskip('websockets')

from tangentswarm.cloud import client as C
from tangentswarm.cloud import handlers, hub, sessions


def test_classify_status():
    cs = sessions.classify_status
    assert cs('ready', None, []) == 'ready'
    assert cs(None, 'Idle', []) == 'idle'
    assert cs(None, 'Awaiting input', []) == 'awaiting'
    assert cs(None, None, ['#12 · Merged']) == 'idle'
    assert cs(None, None, ['#12 · Open']) == 'ci'
    assert cs(None, None, ['checks failed']) == 'ci-fail'
    assert cs(None, None, ['hello']) == 'unknown'


def test_summarize_rows():
    raw = [{'text': '1469. Prove X\nmore', 'name': '1469. Prove X', 'slug': 'session_a',
            'kind': 'running', 'indicatorLabel': None, 'ariaLabels': []},
           {'text': '', 'name': '', 'slug': '', 'kind': None, 'indicatorLabel': None, 'ariaLabels': []}]
    (s,) = sessions.summarize_rows(raw)
    assert s['name'] == '1469. Prove X' and s['status'] == 'running' and 'kind=running' in s['rawSignals']


class FakeSessions:
    """Stands in for tangentswarm.cloud.sessions against a fake page."""

    def __init__(self):
        self.chats = {'1469. Prove X': 'running', '1500. Prove Y': 'awaiting'}
        self.latest = {'1469. Prove X': 'old reply', '1500. Prove Y': None}
        self.open = None
        self.sent = []

    async def list_sessions(self, page):
        return [{'name': n, 'slug': 'session_' + n[:4], 'status': s} for n, s in self.chats.items()]

    async def get_session_status(self, page, name):
        return self.chats.get(name, 'unknown')

    async def open_session(self, page, name):
        self.open = name

    async def send_message(self, page, text):
        self.sent.append((self.open, text))
        self.latest[self.open] = text            # transcript echoes the user's message first
        self.chats[self.open] = 'running'

        async def reply():
            await asyncio.sleep(0.3)
            self.latest[self.open] = f'reply to {text}'
            self.chats[self.open] = 'ready'
        asyncio.get_running_loop().create_task(reply())

    async def get_latest_response(self, page):
        return self.latest.get(self.open)


@pytest.fixture
def fake_hub():
    fs = FakeSessions()

    class Handle:
        page = object()

    async def dispatch(deps, c, req):
        return await handlers.dispatch(deps, c, req, sessions=fs)

    return fs, hub.Hub(Handle(), dispatch_fn=dispatch, open_session=fs.open_session)


async def _with_server(h, fn):
    loop = asyncio.get_running_loop()
    ready = loop.create_future()
    task = asyncio.create_task(hub.serve_hub(h, '127.0.0.1', 0, ready=ready))
    port = await ready
    try:
        return await fn(f'ws://127.0.0.1:{port}/ws')
    finally:
        task.cancel()


def test_protocol_smoke(fake_hub):
    """Port of scialect src/smoke.mts. Checks hello, ping, list, use and use-missing."""
    fs, h = fake_hub
    events = []

    async def go(url):
        async with C.HubClient(url, on_event=events.append) as cl:
            pong = await cl.call({'kind': 'ping'})
            lst = await cl.call({'kind': 'list'})
            use = await cl.call({'kind': 'use', 'chatId': '1500. Prove Y'})
            bad = await cl.call({'kind': 'use', 'chatId': 'nope'})
            nochat = await C.HubClient(url).connect()
            err = await nochat.call({'kind': 'latest'})
            await nochat.close()
            return pong, lst, use, bad, err

    pong, lst, use, bad, err = asyncio.run(_with_server(h, go))
    assert any(e.get('type') == 'hello' for e in events)
    assert pong['kind'] == 'pong'
    assert lst['kind'] == 'list' and len(lst['chats']) == 2 and lst['chats'][0]['transport'] == 'cloud'
    assert use['kind'] == 'use' and use['active']['id'] == '1500. Prove Y'
    assert bad['kind'] == 'error' and 'no chat' in bad['message']
    assert err == {'id': err['id'], 'kind': 'error', 'message': 'no active chat'}


def test_cloud_mcp_helpers(fake_hub):
    fs, h = fake_hub

    async def go(url):
        listed = await C.cloud_list_sessions(url)
        latest = await C.cloud_get_latest_response('1469. Prove X', url)
        sent = await C.cloud_send_message('1500. Prove Y', 'hi', url)
        waited = await C.cloud_wait_for_response('1469. Prove X', 'status?', timeout_sec=5, poll_ms=250, url=url)
        return listed, latest, sent, waited

    listed, latest, sent, waited = asyncio.run(_with_server(h, go))
    assert len(listed['sessions']) == 2 and listed['summary'][0].startswith('1469. Prove X [running]')
    assert latest['text'] == 'old reply'
    assert sent == {'sent_to': '1500. Prove Y'} and ('1500. Prove Y', 'hi') in fs.sent
    assert waited['settled'] and waited['text'] == 'reply to status?' and waited['status'] == 'ready'


def test_wait_for_response_times_out(fake_hub):
    fs, h = fake_hub
    fs.chats['1500. Prove Y'] = 'running'

    async def go(url):
        return await C.cloud_wait_for_response('1500. Prove Y', None, timeout_sec=1, poll_ms=250, url=url)

    res = asyncio.run(_with_server(h, go))
    assert res['settled'] is False


def test_unreachable_hub_reports_error():
    with pytest.raises(C.HubError, match='ws connect failed'):
        asyncio.run(C.cloud_list_sessions('ws://127.0.0.1:9/ws'))
