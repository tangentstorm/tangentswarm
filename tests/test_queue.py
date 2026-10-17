"""Tests for the agent-to-hub message queue: the SQLite layer directly, and
the HTTP hub + client end to end (one agent sends, one consumer reads)."""
import threading
import time

import pytest

from tangentswarm.queue import client
from tangentswarm.queue.db import Queue
from tangentswarm.queue.hub import run_hub


# ---------------------------------------------------------------------------
# db.Queue

@pytest.fixture
def q():
    queue = Queue(':memory:')
    yield queue
    queue.close()


def test_send_requires_known_type(q):
    with pytest.raises(ValueError):
        q.send('alice', 'bob', 'nonsense', 'hi')


def test_send_requires_fields(q):
    with pytest.raises(ValueError):
        q.send('', 'bob', 'status', 'hi')
    with pytest.raises(ValueError):
        q.send('alice', 'bob', 'status', '')


def test_send_and_read_since_cursor(q):
    mid = q.send('agent-1', 'michal', 'status', 'halfway done')
    assert mid == 1
    msgs = q.read('michal')
    assert len(msgs) == 1
    assert msgs[0]['id'] == mid
    assert msgs[0]['sender'] == 'agent-1'
    assert msgs[0]['body'] == 'halfway done'
    assert msgs[0]['read'] == 0


def test_ordering_is_by_id_not_timestamp(q):
    ids = [q.send('a', 'bob', 'status', f'msg {i}') for i in range(5)]
    msgs = q.read('bob')
    assert [m['id'] for m in msgs] == ids


def test_cursor_advances_and_filters_future_reads(q):
    q.send('a', 'bob', 'status', 'first')
    second = q.send('a', 'bob', 'status', 'second')
    assert q.advance_cursor('bob', second) == second
    q.send('a', 'bob', 'status', 'third')
    msgs = q.read('bob')   # default since = reader's cursor
    assert [m['body'] for m in msgs] == ['third']


def test_cursor_never_moves_backward(q):
    first = q.send('a', 'bob', 'status', 'first')
    q.send('a', 'bob', 'status', 'second')
    q.advance_cursor('bob', 999)   # pretend we're way ahead
    assert q.advance_cursor('bob', first) == 999   # does not regress


def test_per_reader_cursors_are_independent(q):
    q.send('a', 'bob', 'status', 'one')
    q.advance_cursor('bob', 1)
    # a second reader watching the same recipient hasn't advanced yet
    assert len(q.read('watcher-bot', recipient='bob')) == 1
    assert len(q.read('bob')) == 0


def test_unread_only_filter_is_independent_of_cursor(q):
    mid = q.send('a', 'bob', 'status', 'one')
    q.advance_cursor('bob', mid)
    assert q.read('bob') == []
    assert q.read('bob', since=0, unread_only=True)[0]['id'] == mid
    q.mark_read(mid)
    assert q.read('bob', since=0, unread_only=True) == []


def test_recipient_filter(q):
    q.send('a', 'bob', 'status', 'for bob')
    q.send('a', 'alice', 'status', 'for alice')
    msgs = q.read('watcher', recipient='bob', since=0)
    assert [m['recipient'] for m in msgs] == ['bob']


def test_ttl_expiry(q):
    q.ttl_days = 0   # everything older than "now" is expired
    q.send('a', 'bob', 'status', 'stale')
    time.sleep(0.01)
    q.send('a', 'bob', 'status', 'triggers sweep')   # expire() runs on send
    msgs = q.read('bob', since=0)
    assert [m['body'] for m in msgs] == ['triggers sweep']


# ---------------------------------------------------------------------------
# hub + client, over real HTTP

class InterleaveDetector:
    """Wraps the queue's sqlite connection and records when one thread runs a statement
    while another thread's statements are still uncommitted. That is exactly the
    expire/insert/commit interleaving the Queue lock must prevent."""

    def __init__(self, conn):
        self.conn = conn
        self.open_by = None
        self.interleaved = 0
        self.guard = threading.Lock()

    def execute(self, *args):
        me = threading.get_ident()
        with self.guard:
            if self.open_by not in (None, me):
                self.interleaved += 1
            self.open_by = me
        time.sleep(0.0005)          # widen the window so a missing lock shows up every run
        return self.conn.execute(*args)

    def commit(self):
        with self.guard:
            self.open_by = None
        return self.conn.commit()

    def close(self):
        return self.conn.close()


def test_concurrent_sends_are_serialized(tmp_path):
    q = Queue(str(tmp_path / 'hub.sqlite3'))
    q.conn = InterleaveDetector(q.conn)
    errors = []

    def worker(n):
        try:
            for i in range(20):
                q.send(f'agent-{n}', 'ops', 'status', f'{n}:{i}')
        except Exception as e:          # pragma: no cover - reported below
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert errors == [] and q.conn.interleaved == 0
    msgs = q.read('checker', since=0, limit=1000)
    assert len(msgs) == 160 and len({m['id'] for m in msgs}) == 160
    assert {m['body'] for m in msgs} == {f'{n}:{i}' for n in range(8) for i in range(20)}
    q.close()


@pytest.fixture
def hub_url(tmp_path):
    db = str(tmp_path / 'hub.sqlite3')
    port_box = {}
    ready = threading.Event()

    def on_ready(port):
        port_box['port'] = port
        ready.set()

    t = threading.Thread(target=run_hub, args=(db,), kwargs={'port': 0, 'ready': on_ready}, daemon=True)
    t.start()
    assert ready.wait(5), 'hub did not start'
    yield f"http://127.0.0.1:{port_box['port']}"
    # daemon thread; process teardown is enough for a test server


def test_end_to_end_one_agent_sends_one_consumer_reads(hub_url):
    result = client.send_message('agent-1', 'michal', 'status', 'build passed',
                                  context='repo=platform branch=work', hub_url=hub_url)
    mid = result['id']
    assert mid >= 1

    msgs = client.read_messages('michal', since=0, hub_url=hub_url)
    assert len(msgs) == 1
    assert msgs[0]['sender'] == 'agent-1'
    assert msgs[0]['body'] == 'build passed'
    assert msgs[0]['context'] == 'repo=platform branch=work'

    client.advance_cursor('michal', mid, hub_url=hub_url)
    assert client.read_messages('michal', hub_url=hub_url) == []

    client.ack(mid, hub_url=hub_url)
    msgs_after_ack = client.read_messages('michal', since=0, unread_only=True, hub_url=hub_url)
    assert msgs_after_ack == []


def test_multiple_readers_independent_cursors_over_http(hub_url):
    client.send_message('agent-1', 'bob', 'question', 'need input', hub_url=hub_url)
    client.advance_cursor('bob', 1, hub_url=hub_url)
    assert client.read_messages('bob', hub_url=hub_url) == []
    assert len(client.read_messages('watcher-bot', recipient='bob', hub_url=hub_url)) == 1


def test_send_rejects_bad_type_over_http(hub_url):
    with pytest.raises(client.HubError):
        client.send_message('agent-1', 'bob', 'not-a-type', 'x', hub_url=hub_url)


def test_concurrent_sends_over_http(hub_url):
    errors, ids = [], []
    lock = threading.Lock()

    def worker(n):
        try:
            for i in range(15):
                mid = client.send_message(f'agent-{n}', 'ops', 'status', f'{n}:{i}', hub_url=hub_url)['id']
                with lock:
                    ids.append(mid)
        except Exception as e:          # pragma: no cover - reported below
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert errors == []
    assert len(ids) == 150 and len(set(ids)) == 150
    msgs = client.read_messages('checker', recipient='ops', since=0, limit=1000, hub_url=hub_url)
    assert sorted(m['id'] for m in msgs) == sorted(ids)
