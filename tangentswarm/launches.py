"""swarm-launch: start a worker Claude, queue it when memory holds it, start queued ones later.

swarm-start-scoped exits 3 ("held") when the worker slice is short of memory. Before
this module nothing recorded a held launch, so nothing retried it and nobody saw the
pressure. swarm-launch runs swarm-start-scoped and, on a hold, adds the launch to a
queue file. swarm-tick then calls start_next() on each pass: when memory admits a
launch, it starts the first queued one (priority milestone first, then the oldest
hold), one launch per pass, and sends the new Claude one line that points to its brief.

Memory is read live on every check: the slice's MemoryCurrent, MemoryHigh and
MemoryMax from systemd, and MemAvailable from /proc/meminfo. Nothing assumes a fixed
slice size. The admission rule is the same as swarm-start-scoped's.

    swarm-launch BRANCH [WINDOW] [--pr N] [--brief FILE]   start, or queue on a hold
    swarm-launch --list                                    print the queue and memory
    swarm-launch --next                                    start the first queued launch if memory admits it
    swarm-launch --drop BRANCH                             remove a queued launch
"""
import argparse
import contextlib
import fcntl
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HOME = Path.home()
BIN = Path(os.environ.get('SWARM_TICK_BIN', str(HOME / 'bin')))
QUEUE = Path(os.environ.get('SWARM_HELD', str(HOME / 'var' / 'swarm-held.json')))
SLICE = os.environ.get('SWARM_SLICE', 'swarm-workers.slice')
MB = 1024 ** 2
FLOOR = 700 * MB              # headroom under MemoryHigh (swarm-start-scoped's rule)
AVAIL_MIN = int(1.5 * 1024 ** 3)  # host MemAvailable must exceed this (same rule)
HELD = 3


# ---------------------------------------------------------------------------
# memory, read live

def _systemctl_show(slice_):
    r = subprocess.run(['systemctl', '--user', 'show', slice_, '-p', 'MemoryCurrent', '-p', 'MemoryHigh',
                        '-p', 'MemoryMax'], capture_output=True, text=True, timeout=30)
    return r.stdout


def _meminfo():
    return Path('/proc/meminfo').read_text()


def parse_limit(v):
    """systemd prints 'infinity' or '[not set]' for no limit."""
    return int(v) if v and v.isdigit() else None


def memory(show=_systemctl_show, meminfo=_meminfo):
    """{current, high, max, avail} in bytes; high/max are None when unlimited."""
    vals = dict(line.split('=', 1) for line in show(SLICE).splitlines() if '=' in line)
    info = {line.split(':')[0]: int(line.split()[1]) * 1024 for line in meminfo().splitlines()
            if line.startswith('MemAvailable:')}
    cur = parse_limit(vals.get('MemoryCurrent'))
    if cur is None:
        raise RuntimeError(f"cannot read {SLICE} MemoryCurrent: {vals.get('MemoryCurrent')!r}")
    return dict(current=cur, high=parse_limit(vals.get('MemoryHigh')), max=parse_limit(vals.get('MemoryMax')),
                avail=info.get('MemAvailable', 0))


def headroom(m):
    limit = m['high'] or m['max']
    return None if limit is None else limit - m['current']


def admits(m):
    room = headroom(m)
    return (room is None or room >= FLOOR) and m['avail'] > AVAIL_MIN


def describe(m):
    def mb(v):
        return 'none' if v is None else f"{v // MB} MB"
    return (f"{SLICE} uses {mb(m['current'])} of MemoryHigh {mb(m['high'])} (MemoryMax {mb(m['max'])}), "
            f"headroom {mb(headroom(m))}; host MemAvailable {mb(m['avail'])}")


# ---------------------------------------------------------------------------
# the queue

@contextlib.contextmanager
def locked_queue(path=None):
    path = path or QUEUE
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_suffix('.lock'), 'a') as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            rows = json.loads(path.read_text())
            if not isinstance(rows, list):
                rows = []
        except (OSError, ValueError):
            rows = []
        before = json.dumps(rows)
        yield rows
        if json.dumps(rows) != before:
            tmp = path.with_suffix('.tmp')
            tmp.write_text(json.dumps(rows, indent=1))
            os.replace(tmp, path)


def read_queue():
    with locked_queue() as rows:
        return list(rows)


def hold(branch, window=None, pr=None, brief=None, now=None):
    """Add a held launch, or refresh its details if it is already queued (keeps its place)."""
    with locked_queue() as rows:
        for r in rows:
            if r['branch'] == branch:
                r.update({k: v for k, v in dict(window=window, pr=pr, brief=brief).items() if v})
                return r
        r = dict(branch=branch, window=window, pr=pr, brief=brief, held_at=now or time.time())
        rows.append(r)
        return r


def drop(branch):
    with locked_queue() as rows:
        n = len(rows)
        rows[:] = [r for r in rows if r['branch'] != branch]
        return n != len(rows)


def ordered(rows, rank=lambda pr: 1):
    """Priority milestone first (rank 0), then the oldest hold."""
    return sorted(rows, key=lambda r: (rank(r.get('pr')), r.get('held_at', 0)))


# ---------------------------------------------------------------------------
# launching

def start_scoped(branch, window=None):
    """Run swarm-start-scoped. Returns (rc, json dict or {'refused': text})."""
    r = subprocess.run([str(BIN / 'swarm-start-scoped'), branch, *([window] if window else [])],
                       capture_output=True, text=True, timeout=300)
    try:
        out = json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        out = {'refused': (r.stderr or r.stdout).strip()[-300:] or f'rc={r.returncode}'}
    return r.returncode, out


def brief_line(path):
    return (f"Your brief is in {path}. Read the whole file first, then follow it. "
            "It names your branch, the issue, the tests and how to report.")


def send_brief(pane, unit, path):
    """Wait for the new Claude's empty prompt in its scope, then send one line that points to the brief."""
    sys.path.insert(0, str(BIN))
    import swarm_parking as sp
    host = sp.Host()
    ok, why = sp.wait_ready(host, pane, unit)
    if not ok:
        raise RuntimeError(why)
    host.tell(pane, brief_line(path))


def launch(branch, window=None, pr=None, brief=None, start=start_scoped, send=send_brief):
    """Start a worker. Returns (what, detail): what is 'launched', 'held' or 'refused'."""
    rc, out = start(branch, window)
    if rc == HELD and out.get('held'):
        hold(branch, out.get('window') or window, pr, brief)
        return 'held', out
    if rc != 0:
        return 'refused', out
    drop(branch)
    if brief:
        try:
            send(out['pane'], out['unit'], brief)
        except Exception as e:  # Claude runs; only the first message failed
            return 'launched', dict(out, brief_error=str(e))
    return 'launched', out


def start_next(rank=lambda pr: 1, mem=memory, start=start_scoped, send=send_brief):
    """Start at most one queued launch if memory admits it.

    Returns (what, row, detail): what is 'empty', 'waiting' (memory holds it), or
    launch()'s result for the first queued row."""
    rows = ordered(read_queue(), rank)
    if not rows:
        return 'empty', None, None
    m = mem()
    if not admits(m):
        return 'waiting', rows[0], describe(m)
    r = rows[0]
    what, out = launch(r['branch'], r.get('window'), r.get('pr'), r.get('brief'), start=start, send=send)
    if what == 'refused':
        drop(r['branch'])  # a refusal (busy window, missing worktree) needs judgment, not a retry
    return what, r, out


def main(argv=None):
    ap = argparse.ArgumentParser(prog='swarm-launch', description=__doc__.split('\n', 1)[0])
    ap.add_argument('branch', nargs='?')
    ap.add_argument('window', nargs='?')
    ap.add_argument('--pr', type=int)
    ap.add_argument('--brief', help='brief file; the new Claude is told to read it')
    g = ap.add_mutually_exclusive_group()
    g.add_argument('--list', action='store_true')
    g.add_argument('--next', action='store_true')
    g.add_argument('--drop', metavar='BRANCH')
    a = ap.parse_args(argv)
    try:
        if a.list:
            print(describe(memory()) + ('; admits a launch' if admits(memory()) else '; holds launches'))
            for r in ordered(read_queue()):
                print(json.dumps(r))
            return 0
        if a.drop:
            return 0 if drop(a.drop) else 1
        if a.next:
            what, r, out = start_next()
            print(json.dumps(dict(what=what, row=r, detail=out)))
            return 0 if what in ('launched', 'empty', 'waiting') else 1
        if not a.branch:
            ap.error('BRANCH is required')
        if a.brief and not Path(a.brief).is_file():
            ap.error(f'{a.brief} is not a file')
        what, out = launch(a.branch, a.window, a.pr, a.brief and str(Path(a.brief).resolve()))
        print(json.dumps(dict(what=what, **out)))
        return {'launched': 0, 'held': HELD}.get(what, 1)
    except (RuntimeError, subprocess.TimeoutExpired, OSError) as e:
        print(f"swarm-launch error: {e}", file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
