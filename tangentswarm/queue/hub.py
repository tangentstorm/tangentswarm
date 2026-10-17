"""Hub server: a small HTTP wrapper over queue.db.Queue, meant to run as a
persistent process (`swarm-hub`). Binds 127.0.0.1 by default; there is no
authentication yet, so do not expose it beyond localhost.

    swarm-hub                       # serves on http://127.0.0.1:8777
    swarm-hub --port 9000 --db p.db

Routes (all JSON):
    POST /send    {sender, recipient, type, body, context?}  -> {id}
    GET  /read    ?reader=&recipient=&since=&unread_only=&limit=  -> {messages}
    POST /cursor  {reader, last_id}                           -> {last_id}
    POST /ack     {id}                                        -> {ok}
    GET  /health                                              -> {ok}

Control verbs (start, stop, interrupt, new-conversation) are not handled
here yet -- see the TODO at the bottom of this module.
"""
import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .db import Queue

DEFAULT_PORT = 8777


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def make_handler(queue):
    class Handler(BaseHTTPRequestHandler):
        def _json(self, code, obj):
            data = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self):
            length = int(self.headers.get('Content-Length', 0) or 0)
            raw = self.rfile.read(length) if length else b'{}'
            return json.loads(raw or b'{}')

        def do_GET(self):
            parts = urlsplit(self.path)
            qs = {k: v[0] for k, v in parse_qs(parts.query).items()}
            try:
                if parts.path == '/health':
                    return self._json(200, {'ok': True})
                if parts.path == '/read':
                    reader = qs.get('reader')
                    if not reader:
                        return self._json(400, {'error': 'reader is required'})
                    unread_only = qs.get('unread_only', '').lower() in ('1', 'true', 'yes')
                    msgs = queue.read(reader, recipient=qs.get('recipient'),
                                       since=qs.get('since'), unread_only=unread_only,
                                       limit=qs.get('limit', 100))
                    return self._json(200, {'messages': msgs})
                return self._json(404, {'error': f'no such route: GET {parts.path}'})
            except (KeyError, ValueError, TypeError) as e:
                self._json(400, {'error': str(e)})
            except Exception as e:
                self._json(500, {'error': str(e)})

        def do_POST(self):
            parts = urlsplit(self.path)
            try:
                body = self._body()
                if parts.path == '/send':
                    mid = queue.send(body.get('sender'), body.get('recipient'), body.get('type'),
                                      body.get('body'), body.get('context'))
                    return self._json(200, {'id': mid})
                if parts.path == '/cursor':
                    last_id = queue.advance_cursor(body['reader'], body['last_id'])
                    return self._json(200, {'last_id': last_id})
                if parts.path == '/ack':
                    queue.mark_read(body['id'])
                    return self._json(200, {'ok': True})
                return self._json(404, {'error': f'no such route: POST {parts.path}'})
            except (KeyError, ValueError, TypeError) as e:
                self._json(400, {'error': str(e)})
            except Exception as e:
                self._json(500, {'error': str(e)})

        def log_message(self, fmt, *args):
            log(f"[hub] {self.address_string()} {fmt % args}")

    return Handler


def run_hub(db_path, host='127.0.0.1', port=DEFAULT_PORT, ttl_days=30, ready=None):
    queue = Queue(db_path, ttl_days=ttl_days)
    server = ThreadingHTTPServer((host, port), make_handler(queue))
    actual = server.server_address[1]
    log(f"[tangentswarm-hub] http://{host}:{actual} (db={db_path}, ttl_days={ttl_days})")
    if ready is not None:
        ready(actual)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        queue.close()


def default_db_path():
    from .. import paths
    paths.ensure_private_dir(paths.state_dir())
    return str(paths.state_dir() / 'hub.sqlite3')


def main(argv=None):
    ap = argparse.ArgumentParser(prog='swarm-hub', description='agent-to-hub message queue server')
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--port', type=int, default=DEFAULT_PORT)
    ap.add_argument('--db', default=None, help='sqlite path (default: XDG state dir)')
    ap.add_argument('--ttl-days', type=int, default=30)
    a = ap.parse_args(argv)
    run_hub(a.db or default_db_path(), a.host, a.port, a.ttl_days)


if __name__ == '__main__':
    main()


# TODO (next increment): control verbs. Design calls for the hub to execute
# start / stop / interrupt / new-conversation pulled off the queue, using
# tangentswarm.registry.start_agent and tangentswarm.agents.tell_agent the
# same way swarm-mcp's start_agent/tell_agent tools do, so operators and
# agents can drive routine control through the queue instead of tmux
# keystrokes. Needs: a message "type" or route for control verbs (the
# current type enum is status/blocker/question/done), a mapping from
# recipient name -> tmux target (reusing tangentswarm.workers.WorkerConfig /
# known-agents.jsonl), and a decision on sync (reply inline) vs async
# (reply posted back onto the queue) semantics.
