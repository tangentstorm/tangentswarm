"""swarm-hookgate: cheap front gate for the CI-completion GitHub hook.

The hook runs `ci-last-check SHA` first and stops when it exits 1. A storm of CI
events on new draft PRs once used up the whole REST quota, because every event ran
the REST check, hr-gate and flow-status. This gate runs before any REST call:

  1. Map SHA to a PR with a cached board snapshot (one GraphQL query, refreshed only
     when older than SNAPSHOT_TTL or when the SHA is not a known head).
  2. Skip a closed PR, a draft PR, and a card outside GATE_COLS (In Progress and Ready
     included), except the one PR that swarm-flow is pulling into Staging.
  3. Debounce per PR head: a head whose CI was already seen complete is skipped for
     DONE_DEBOUNCE seconds; repeated events while CI still runs are collapsed to one
     REST check per BUSY_DEBOUNCE seconds.
  4. Check the REST quota (the rate_limit endpoint costs nothing). Below RESERVE calls
     left, wait for the reset time instead of failing in the middle of a run.

Commands:
  swarm-hookgate ci-last-check SHA   gate, then hr-gate's REST check; exit 0 = CI done
                                     on a gated head (go on), 1 = stop (skipped or
                                     still running), 2 = error
  swarm-hookgate check SHA           only steps 1-3; exit 0 = go on, 1 = skip
  swarm-hookgate wait                only step 4 (run before hr-gate --apply)
"""
import argparse
import contextlib
import fcntl
import json
import os
import runpy
import subprocess
import sys
import time
from pathlib import Path

HOME = Path.home()
BIN = Path(os.environ.get('SWARM_TICK_BIN', str(HOME / 'bin')))
CACHE = Path(os.environ.get('SWARM_HOOK_CACHE', str(HOME / '.cache' / 'swarm-hook')))
GATE_COLS = ('Auto Review', 'Pending')
SNAPSHOT_TTL = int(os.environ.get('SWARM_HOOK_SNAPSHOT_TTL', '120'))
DONE_DEBOUNCE = int(os.environ.get('SWARM_HOOK_DONE_DEBOUNCE', '1800'))
BUSY_DEBOUNCE = int(os.environ.get('SWARM_HOOK_BUSY_DEBOUNCE', '60'))
RESERVE = int(os.environ.get('SWARM_HOOK_RESERVE', '300'))
MAX_WAIT = int(os.environ.get('SWARM_HOOK_MAX_WAIT', '3900'))


def pulled():
    """The PR that swarm-flow is pulling into Staging (it sits in Ready until it passes)."""
    from . import flow
    return flow.pulled_pr()


class Stop(Exception):
    """The hook should stop here (exit 1); the message says why."""


# ---------------------------------------------------------------------------
# locked JSON files

@contextlib.contextmanager
def locked_json(path):
    """Yield a dict loaded from path under an exclusive flock; it is saved on exit."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_suffix('.lock'), 'a') as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            data = json.loads(path.read_text())
            if not isinstance(data, dict):
                data = {}
        except (OSError, ValueError):
            data = {}
        yield data
        tmp = path.with_suffix('.tmp')
        tmp.write_text(json.dumps(data, sort_keys=True))
        os.replace(tmp, path)


# ---------------------------------------------------------------------------
# board snapshot (GraphQL only)

def gh(*args, input=None, timeout=120):
    r = subprocess.run(['gh', *args], capture_output=True, text=True, input=input, timeout=timeout)
    if r.returncode:
        raise RuntimeError(f"gh {' '.join(args[:2])}...: {(r.stderr or r.stdout).strip()[:200]}")
    return r.stdout


def gql(q, **v):
    args = ['api', 'graphql', '-f', f'query={q}']
    for k, val in v.items():
        if val is not None:
            args += ['-F', f'{k}={val}']
    return json.loads(gh(*args))['data']


def fetch_board():
    """{number: {col, draft, head, state}} for the board's PRs, from one paged GraphQL query."""
    from . import tick
    prs = tick.board_prs({'gql': gql, 'PARKED': set()})
    return {str(n): dict(col=p['col'], draft=p['draft'], head=p['head'], state=p['state']) for n, p in prs.items()}


def by_head(prs, sha):
    for n, p in prs.items():
        if p['head'] and (p['head'] == sha or (len(sha) >= 7 and p['head'].startswith(sha))):
            return int(n), p
    return None, None


def find_pr(sha, now, fetch=fetch_board):
    """(pr number, pr) for this head, refreshing the snapshot only when it is stale or misses."""
    with locked_json(CACHE / 'board.json') as snap:
        fresh = now - snap.get('at', 0) < SNAPSHOT_TTL
        n, p = by_head(snap.get('prs', {}), sha)
        if n is None or not fresh:
            snap['prs'], snap['at'] = fetch(), now
            n, p = by_head(snap['prs'], sha)
        return n, p


# ---------------------------------------------------------------------------
# the gate

def check(sha, now=None, fetch=fetch_board):
    """Raise Stop if the hook should not spend REST calls on this head; else return the PR number."""
    now = time.time() if now is None else now
    n, p = find_pr(sha, now, fetch)
    if n is None:
        raise Stop(f"{sha[:8]} is not the head of any open board PR")
    if p['state'] != 'OPEN':
        raise Stop(f"#{n} is {p['state'].lower()}")
    if p['draft']:
        raise Stop(f"#{n} is a draft")
    if p['col'] not in GATE_COLS and n != pulled():
        raise Stop(f"#{n} is in {p['col'] or 'no column'}, not {' or '.join(GATE_COLS)}")
    with locked_json(CACHE / 'state.json') as st:
        seen = st.setdefault('heads', {})
        for k in [k for k, v in seen.items() if now - v.get('at', 0) > DONE_DEBOUNCE]:
            del seen[k]
        prev = seen.get(sha)
        if prev and prev.get('done'):
            raise Stop(f"#{n} head {sha[:8]}: CI already seen complete at {int(now - prev['at'])}s ago")
        if prev and now - prev['at'] < BUSY_DEBOUNCE:
            raise Stop(f"#{n} head {sha[:8]}: checked {int(now - prev['at'])}s ago; CI was still running")
        seen[sha] = dict(at=now, pr=n, done=False)
    return n


def record(sha, done, now=None):
    now = time.time() if now is None else now
    with locked_json(CACHE / 'state.json') as st:
        h = st.setdefault('heads', {}).setdefault(sha, {})
        h.update(at=now, done=bool(done))


def rate_core():
    """(remaining, reset epoch) for the REST quota. The rate_limit endpoint is free."""
    core = json.loads(gh('api', 'rate_limit'))['resources']['core']
    return int(core['remaining']), int(core['reset'])


def wait_for_quota(reserve=RESERVE, max_wait=MAX_WAIT, rate=rate_core, sleep=time.sleep, now=time.time,
                   log=lambda s: print(s, file=sys.stderr)):
    """Return once at least `reserve` REST calls are left; sleep through the reset if not."""
    deadline = now() + max_wait
    while True:
        left, reset = rate()
        if left >= reserve:
            return left
        pause = max(reset - now(), 0) + 5
        if now() + pause > deadline:
            raise RuntimeError(f"REST quota {left} left; reset in {int(pause)}s is past the {max_wait}s wait limit")
        log(f"swarm-hookgate: REST quota {left} left (< {reserve}); waiting {int(pause)}s for the reset")
        sleep(pause)


def ci_last_check(sha):
    """hr-gate's REST check: 0 if every check on this head finished, 1 if any still runs."""
    g = runpy.run_path(str(BIN / 'hr-gate'), run_name='ci_last_check')
    return g['ci_last_check'](sha)


def main(argv=None):
    ap = argparse.ArgumentParser(prog='swarm-hookgate', description=__doc__.split('\n', 1)[0])
    ap.add_argument('cmd', choices=['ci-last-check', 'check', 'wait'])
    ap.add_argument('sha', nargs='?')
    a = ap.parse_args(argv)
    if a.cmd != 'wait' and not a.sha:
        ap.error(f'{a.cmd} needs SHA')
    try:
        if a.cmd == 'wait':
            print(f"REST quota ok: {wait_for_quota()} left")
            return 0
        n = check(a.sha)
        if a.cmd == 'check':
            print(f"go: #{n} head {a.sha[:8]}")
            return 0
        wait_for_quota()
        rc = ci_last_check(a.sha)
        record(a.sha, done=rc == 0)
        print(f"#{n} head {a.sha[:8]}: " + ('CI complete' if rc == 0 else 'CI still running'))
        return rc
    except Stop as e:
        print(f"skip: {e}")
        return 1
    except (RuntimeError, subprocess.TimeoutExpired) as e:
        print(f"swarm-hookgate error: {e}", file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
