"""swarm-lock: the one global gate lock. A second caller exits at once with "busy".

Only one merge or gate operation may run at a time across the swarm: hr-gate --apply,
hr-gate --sweep, pending-promote and the work-merge step. Overlapping runs once waited
on each other for more than 10 minutes and used up the REST quota. So every one of
them takes this lock with a non-blocking flock; a caller that finds it held prints
"busy: <holder>" and exits EXIT_BUSY (75, EX_TEMPFAIL) without any API call. The next
event or pass tries again.

Contract (hr-gate and pending-promote implement the same one inline):
  lock file    $SWARM_GATE_LOCK (default ~/.cache/swarm-serial.lock);
               flock(LOCK_EX | LOCK_NB)
  holder note  <lock file>.holder: one line naming the holder, written after the lock
               is taken (only for the "busy" message; the flock is the truth)
  child runs   SWARM_SERIAL_HELD=1 (or the older HR_GATE_LOCK_HELD=1) in the
               environment: this process or a parent already holds the lock
  who locks    only a run that changes state (wants_lock): hr-gate --apply or --demote,
               pending-promote --apply. Never a dry run, a report (hr-gate --sweep alone),
               -h/--help, or a script that only loads hr-gate with runpy.

  queue        a caller that finds the lock busy does not give up: it writes its argv to
               QUEUE_DIR/<hash of argv>.json (one file per distinct command) and exits
               EXIT_BUSY with "queued". The swarm-promoted daemon owns the queue and runs
               each request under the lock, oldest first (drain). Only QUEUE_TOOLS may be
               queued; a request older than QUEUE_MAX_AGE is dropped.

  waiting      to wait for the lock, use swarm-lock --wait SECONDS (flock). Never a
               `until ! pgrep -f pending-promote` loop: -f matches the loop's own command
               line, so it waits forever, and a gap between two runs is not a free lock.

    swarm-lock -n work-merge -- git -C WORKTREE push origin HEAD:work
    swarm-lock --wait 600 -n check -- CMD    wait up to 600 s for the lock, then run CMD
    swarm-lock --wait 600                    wait until the lock is free, then exit 0
    swarm-lock --status
    swarm-lock --queued
"""
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

LOCK = Path(os.environ.get('SWARM_GATE_LOCK', str(Path.home() / '.cache' / 'swarm-serial.lock')))
EXIT_BUSY = 75
QUEUE_DIR = Path(os.environ.get('SWARM_GATE_QUEUE', str(Path.home() / '.cache' / 'swarm-gate-queue')))
QUEUE_TOOLS = ('hr-gate', 'pending-promote', 'swarm-advance')
QUEUE_MAX_AGE = int(os.environ.get('SWARM_GATE_QUEUE_MAX_AGE', '1800'))
HELD_ENV = 'SWARM_SERIAL_HELD'
OLD_HELD_ENV = 'HR_GATE_LOCK_HELD'


def already_held(env):
    return env.get(HELD_ENV) == '1' or bool(env.get(OLD_HELD_ENV))


NO_LOCK_FLAGS = ('-h', '--help', '--dry-run')
MUTATING = {'hr-gate': ('--apply', '--demote'), 'pending-promote': ('--apply',)}


def wants_lock(tool, argv):
    """True if `tool argv...` changes state and so must hold the serial lock."""
    args = list(argv)
    return (any(a in MUTATING.get(tool, ()) for a in args)
            and not any(a in NO_LOCK_FLAGS for a in args))


class Busy(Exception):
    pass


def holder_path(lock=None):
    lock = lock or LOCK
    return lock.with_name(lock.name + '.holder')


def holder(lock=None):
    try:
        return holder_path(lock).read_text().strip() or 'unknown holder'
    except OSError:
        return 'unknown holder'


def _take(f, wait, sleep=time.sleep, clock=time.monotonic):
    """flock(LOCK_EX) on f, trying for up to `wait` seconds (0: one try). True if taken."""
    end = clock() + wait
    while True:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            if clock() >= end:
                return False
            sleep(min(1.0, max(0.05, end - clock())))


@contextlib.contextmanager
def gate_lock(name, lock=None, env=os.environ, wait=0):
    """Hold the global gate lock for the block, or raise Busy (at once, or after `wait`
    seconds). Re-entrant via HELD_ENV."""
    lock = lock or LOCK
    if already_held(env):
        yield
        return
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, 'a') as f:
        if not _take(f, wait):
            raise Busy(holder(lock))
        mine = f"{name} pid {os.getpid()} since {datetime.now().isoformat(timespec='seconds')}\n"
        holder_path(lock).write_text(mine)
        try:
            yield
        finally:
            try:  # a stale note would name us as the holder of someone else's lock
                if holder_path(lock).read_text() == mine:
                    holder_path(lock).unlink()
            except OSError:
                pass
            fcntl.flock(f, fcntl.LOCK_UN)


def is_busy(lock=None):
    lock = lock or LOCK
    if not lock.exists():
        return False
    with open(lock, 'a') as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(f, fcntl.LOCK_UN)
        return False


# ---------------------------------------------------------------------------
# the request queue (the daemon drains it; callers never "busy, give up")

def queue_key(argv):
    return hashlib.sha1('\0'.join(argv).encode()).hexdigest()[:16]


def enqueue(argv, by='', qdir=None, now=None):
    """Queue argv to run under the lock. One file per distinct argv; returns its path."""
    qdir = qdir or QUEUE_DIR
    if os.path.basename(argv[0]) not in QUEUE_TOOLS:
        raise ValueError(f"only {', '.join(QUEUE_TOOLS)} may be queued, not {argv[0]}")
    qdir.mkdir(parents=True, exist_ok=True)
    path = qdir / f"{queue_key(argv)}.json"
    if not path.exists():
        tmp = path.with_suffix('.tmp')
        tmp.write_text(json.dumps(dict(argv=list(argv), at=time.time() if now is None else now, by=by)))
        os.replace(tmp, path)
    return path


def queued(qdir=None):
    """[(path, request)] oldest first. Unreadable files are skipped."""
    qdir = qdir or QUEUE_DIR
    out = []
    for path in qdir.glob('*.json') if qdir.exists() else []:
        try:
            req = json.loads(path.read_text())
            out.append((path, req))
        except (OSError, ValueError):
            continue
    return sorted(out, key=lambda pr: pr[1].get('at', 0))


def drain(run, say, qdir=None, now=None, max_age=None):
    """Run every queued request (the caller holds the lock). The file is removed before the
    run, so a request queued again during the run waits for the next drain. Returns
    [(argv, rc)]. A request older than max_age, or not for QUEUE_TOOLS, is dropped."""
    now = time.time() if now is None else now
    max_age = QUEUE_MAX_AGE if max_age is None else max_age
    done = []
    for path, req in queued(qdir):
        argv = req.get('argv') or []
        try:
            path.unlink()
        except OSError:
            continue  # another drainer took it
        if not argv or os.path.basename(argv[0]) not in QUEUE_TOOLS:
            say(f"queue: dropped a request that is not for {', '.join(QUEUE_TOOLS)}: {argv[:1]}")
            continue
        age = now - req.get('at', now)
        if age > max_age:
            say(f"queue: dropped {' '.join(argv)} (queued {int(age)}s ago by {req.get('by') or '?'})")
            continue
        rc = run(argv)
        say(f"queue: ran {' '.join(argv)} (queued {int(age)}s ago by {req.get('by') or '?'}): rc={rc}")
        done.append((argv, rc))
    return done


def run_held(argv, timeout=1800):
    """Run argv as a child of the lock holder (it must not lock or queue again)."""
    env = dict(os.environ, **{HELD_ENV: '1', OLD_HELD_ENV: '1'})
    return subprocess.run(argv, env=env, timeout=timeout).returncode


def main(argv=None):
    ap = argparse.ArgumentParser(prog='swarm-lock', description=__doc__.split('\n', 1)[0])
    ap.add_argument('-n', '--name', default='swarm-lock', help='holder name shown to a busy caller')
    ap.add_argument('--status', action='store_true', help='print busy or free, and the holder')
    ap.add_argument('--queued', action='store_true', help='list queued requests')
    ap.add_argument('--wait', type=float, default=0, metavar='SECONDS',
                    help='wait up to SECONDS for the lock (flock; use this, not a pgrep loop)')
    ap.add_argument('cmd', nargs=argparse.REMAINDER)
    a = ap.parse_args(argv)
    if a.status:
        busy = is_busy()
        print(f"busy: {holder()}" if busy else 'free')
        return EXIT_BUSY if busy else 0
    if a.queued:
        for _, req in queued():
            print(f"{int(time.time() - req.get('at', 0))}s\t{req.get('by') or '?'}\t{' '.join(req.get('argv') or [])}")
        return 0
    cmd = a.cmd[1:] if a.cmd[:1] == ['--'] else a.cmd
    if not cmd and a.wait:
        try:
            with gate_lock(f"{a.name} (waiter)", wait=a.wait):
                return 0  # the lock was free; released at once
        except Busy as e:
            print(f"busy after {a.wait:g}s: {e}")
            return EXIT_BUSY
    if not cmd:
        ap.error('give a command after --')
    try:
        with gate_lock(f"{a.name} ({' '.join(cmd)[:80]})", wait=a.wait):
            env = dict(os.environ, **{HELD_ENV: '1', OLD_HELD_ENV: '1'})
            return subprocess.run(cmd, env=env).returncode
    except Busy as e:
        print(f"busy: {e}")
        return EXIT_BUSY


if __name__ == '__main__':
    sys.exit(main())
