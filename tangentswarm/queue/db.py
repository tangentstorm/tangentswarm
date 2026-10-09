"""SQLite-backed message queue.

Messages are append-only; ordering is always the autoincrement `id`, never
the timestamp (clocks skew, NTP jumps, two sends in the same millisecond).
`ts` is kept only for the TTL sweep and for humans reading the table.

Two independent notions of "seen" live here on purpose:
  - `read` on a message: the one recipient has consumed it. Boolean, final.
  - a cursor per reader name: how far that reader has polled the log. Several
    readers (a human, a bot, another agent) can each track their own
    position over the same recipient's messages without stepping on each
    other's cursor.
"""
import sqlite3
import time
from pathlib import Path

DEFAULT_TTL_DAYS = 30
MESSAGE_TYPES = ('status', 'blocker', 'question', 'done')

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    sender    TEXT NOT NULL,
    recipient TEXT NOT NULL,
    ts        REAL NOT NULL,
    type      TEXT NOT NULL,
    body      TEXT NOT NULL,
    context   TEXT,
    read      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_messages_recipient ON messages(recipient);
CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(ts);

CREATE TABLE IF NOT EXISTS cursors (
    reader  TEXT PRIMARY KEY,
    last_id INTEGER NOT NULL DEFAULT 0
);
"""


def connect(path):
    if str(path) != ':memory:':
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


class Queue:
    def __init__(self, path, ttl_days=DEFAULT_TTL_DAYS):
        self.conn = connect(path)
        self.ttl_days = ttl_days

    def expire(self):
        """Delete messages older than ttl_days. Called on every send so a
        quiet hub still sheds old rows without a separate scheduler."""
        cutoff = time.time() - self.ttl_days * 86400
        self.conn.execute('DELETE FROM messages WHERE ts < ?', (cutoff,))
        self.conn.commit()

    def send(self, sender, recipient, type, body, context=None):
        if type not in MESSAGE_TYPES:
            raise ValueError(f"type must be one of {MESSAGE_TYPES}, got {type!r}")
        if not sender or not isinstance(sender, str):
            raise ValueError('sender is required')
        if not recipient or not isinstance(recipient, str):
            raise ValueError('recipient is required')
        if not body or not isinstance(body, str):
            raise ValueError('body is required')
        self.expire()
        cur = self.conn.execute(
            'INSERT INTO messages (sender, recipient, ts, type, body, context) VALUES (?, ?, ?, ?, ?, ?)',
            (sender, recipient, time.time(), type, body, context))
        self.conn.commit()
        return cur.lastrowid

    def _cursor(self, reader):
        row = self.conn.execute('SELECT last_id FROM cursors WHERE reader = ?', (reader,)).fetchone()
        return row['last_id'] if row else 0

    def read(self, reader, recipient=None, since=None, unread_only=False, limit=100):
        """Messages after `since` (default: reader's own cursor), oldest first.
        Does not move the cursor -- call advance_cursor once the caller has
        actually processed what it got back."""
        after = self._cursor(reader) if since is None else int(since)
        clauses = ['id > ?']
        params = [after]
        if recipient is not None:
            clauses.append('recipient = ?')
            params.append(recipient)
        if unread_only:
            clauses.append('read = 0')
        sql = f"SELECT * FROM messages WHERE {' AND '.join(clauses)} ORDER BY id ASC LIMIT ?"
        params.append(int(limit))
        rows = self.conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def advance_cursor(self, reader, last_id):
        """Move reader's cursor forward to last_id. Never moves it backward,
        so an out-of-order ack can't un-read a message."""
        last_id = int(last_id)
        self.conn.execute(
            'INSERT INTO cursors (reader, last_id) VALUES (?, ?) '
            'ON CONFLICT(reader) DO UPDATE SET last_id = excluded.last_id '
            'WHERE excluded.last_id > cursors.last_id',
            (reader, last_id))
        self.conn.commit()
        return self._cursor(reader)

    def mark_read(self, message_id):
        self.conn.execute('UPDATE messages SET read = 1 WHERE id = ?', (int(message_id),))
        self.conn.commit()

    def close(self):
        self.conn.close()
