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

    swarm-lock -n work-merge -- git -C WORKTREE push origin HEAD:work
    swarm-lock --status
"""
import argparse
import contextlib
import fcntl
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

LOCK = Path(os.environ.get('SWARM_GATE_LOCK', str(Path.home() / '.cache' / 'swarm-serial.lock')))
EXIT_BUSY = 75
HELD_ENV = 'SWARM_SERIAL_HELD'
OLD_HELD_ENV = 'HR_GATE_LOCK_HELD'


def already_held(env):
    return env.get(HELD_ENV) == '1' or bool(env.get(OLD_HELD_ENV))


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


@contextlib.contextmanager
def gate_lock(name, lock=None, env=os.environ):
    """Hold the global gate lock for the block, or raise Busy at once. Re-entrant via HELD_ENV."""
    lock = lock or LOCK
    if already_held(env):
        yield
        return
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, 'a') as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Busy(holder(lock)) from None
        holder_path(lock).write_text(f"{name} pid {os.getpid()} since {datetime.now().isoformat(timespec='seconds')}\n")
        try:
            yield
        finally:
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


def main(argv=None):
    ap = argparse.ArgumentParser(prog='swarm-lock', description=__doc__.split('\n', 1)[0])
    ap.add_argument('-n', '--name', default='swarm-lock', help='holder name shown to a busy caller')
    ap.add_argument('--status', action='store_true', help='print busy or free, and the holder')
    ap.add_argument('cmd', nargs=argparse.REMAINDER)
    a = ap.parse_args(argv)
    if a.status:
        busy = is_busy()
        print(f"busy: {holder()}" if busy else 'free')
        return EXIT_BUSY if busy else 0
    cmd = a.cmd[1:] if a.cmd[:1] == ['--'] else a.cmd
    if not cmd:
        ap.error('give a command after --')
    try:
        with gate_lock(f"{a.name} ({' '.join(cmd)[:80]})"):
            env = dict(os.environ, **{HELD_ENV: '1', OLD_HELD_ENV: '1'})
            return subprocess.run(cmd, env=env).returncode
    except Busy as e:
        print(f"busy: {e}")
        return EXIT_BUSY


if __name__ == '__main__':
    sys.exit(main())
