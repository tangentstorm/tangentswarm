"""swarm-promoted: refill Human Review within seconds, with no LLM in the path.

A systemd user service. Every POLL seconds (or at once on SIGUSR1, which a webhook
receiver may send) it reads the Project 4 Status column of every PR in one lean
GraphQL query (through the minbot shim, so on the app quota). An OPEN PR counts; a
merged PR whose card still says Human Review does not. When Human Review holds fewer
than 2 and Staging is not empty, it takes the one serial lock (non-blocking; if busy
it retries every RETRY seconds) and:

  1. Fast path: the head of the Staging queue was already gated at its current head
     commit (an hr-gate "apply" PASS or PENDING line with no failed condition) and is
     not a stacked PR. Move its card to Human Review FIRST, and write the hr-gate PASS
     line for that head.
  2. Then the slow work, still under the lock: pending-promote --apply if Human Review
     is still short. With SWARM_PROMOTE_PULL=1, a full swarm-flow step instead, which
     also pulls one Ready PR into Staging.

    swarm-promoted                  run the daemon
    swarm-promoted --once [--dry-run]   one check (and promotion) and exit
"""
import argparse
import os
import signal
import sys
import time
from pathlib import Path

from . import board, gatelock

HOME = Path.home()
BIN = Path(os.environ.get('SWARM_TICK_BIN', str(HOME / 'bin')))
POLL = int(os.environ.get('SWARM_PROMOTE_POLL', '30'))
RETRY = int(os.environ.get('SWARM_PROMOTE_RETRY', '5'))
PULL = os.environ.get('SWARM_PROMOTE_PULL') == '1'  # also run the Staging pull
REPO = 'tangentcode/platform'
HR_TARGET = 2

LEAN_Q = """query($c:String){organization(login:"tangentcode"){projectV2(number:4){items(first:100, after:$c){
 pageInfo{hasNextPage endCursor}
 nodes{id fieldValueByName(name:"Status"){...on ProjectV2ItemFieldSingleSelectValue{name optionId}}
  content{__typename ...on PullRequest{number state headRefOid repository{nameWithOwner}}}}}}}}"""


def say(line):
    print(time.strftime('%H:%M:%S ') + line, flush=True)


def read_columns(gql):
    """{column key: [{number, head, item}]} for OPEN platform PRs, by option id."""
    cols, cursor = {}, None
    while True:
        data = gql(LEAN_Q, c=cursor)['organization']['projectV2']['items']
        for it in data['nodes']:
            c = it.get('content') or {}
            if (c.get('__typename') != 'PullRequest' or c.get('state') != 'OPEN'
                    or (c.get('repository') or {}).get('nameWithOwner') != REPO):
                continue
            cols.setdefault(board.column(it.get('fieldValueByName')), []).append(
                dict(number=c['number'], head=c['headRefOid'], item=it['id']))
        if not data['pageInfo']['hasNextPage']:
            return cols
        cursor = data['pageInfo']['endCursor']


def needs_promotion(cols):
    return len(cols.get(board.HUMAN_REVIEW, [])) < HR_TARGET and bool(cols.get(board.STAGING))


def gated_heads(log_text):
    """{(#n, head8)} that hr-gate let into Staging or Human Review with no failed condition."""
    out = set()
    for line in log_text.splitlines():
        p = line.split('\t')
        if len(p) > 5 and p[1] == 'apply' and p[4] in ('PASS', 'PENDING') and p[5] == 'fail=-':
            out.add((p[2], p[3]))
    return out


def staging_head(cols, queue):
    """The Staging PR that goes next: first in hr-gate's queue order, else the lowest number."""
    staging = {p['number']: p for p in cols.get(board.STAGING, [])}
    for n in [r['pr'] for r in queue]:
        if n in staging:
            return staging[n]
    return staging[min(staging)] if staging else None


class Promoter:
    def __init__(self, g=None, dry_run=False):
        import runpy
        self.g = g if g is not None else runpy.run_path(str(BIN / 'hr-gate'), run_name='swarm_promoted')
        self.dry = dry_run

    def log_text(self):
        try:
            return Path(self.g['LOG']).read_text()
        except OSError:
            return ''

    def fast_path(self, cols):
        """Move the gated Staging head to Human Review. Returns the PR number or None."""
        p = staging_head(cols, self.g['queue_from_board_or_file']())
        if p is None:
            return None
        n, head = p['number'], p['head']
        if (f"#{n}", head[:8]) not in gated_heads(self.log_text()):
            say(f"#{n} is the Staging head but its head {head[:8]} has no gate pass: slow path")
            return None
        full = self.g['fetch'](n, refresh=True)
        if full['headRefOid'] != head or full.get('isDraft'):
            say(f"#{n} changed (head {full['headRefOid'][:8]}, draft={full.get('isDraft')}): slow path")
            return None
        if self.g['stacked_base'](full.get('body') or ''):
            say(f"#{n} is a stacked PR: slow path (pending-promote decides)")
            return None
        if self.dry:
            say(f"would move #{n} Staging -> Human Review (gated at {head[:8]})")
            return n
        self.g['move'](p['item'], board.HUMAN_REVIEW)
        self.g['log']('apply', n, head[:8], 'PASS', [])
        say(f"moved #{n} Staging -> Human Review (fast path, gated at {head[:8]})")
        return n

    def slow_path(self):
        """After the fast move: pending-promote if Human Review is still short. With
        SWARM_PROMOTE_PULL=1, a full swarm-flow step instead (it also pulls into Staging)."""
        from . import flow
        f = flow.Flow(dry_run=self.dry, out=lambda line: None, g=self.g)
        if PULL:
            f.step()
        else:
            _, cols = f.columns()
            f.promote(cols)
        for line in f.did + f.notes:
            say('flow: ' + line)

    def once(self, retry=False):
        """One check. Returns 'idle', 'busy' or 'promoted'. A retry after busy goes straight
        to the lock (the read under the lock is fresh), so a long busy spell costs no reads."""
        if not retry:
            cols = read_columns(self.g['gql'])
            if not needs_promotion(cols):
                return 'idle'
            hr, st = len(cols.get(board.HUMAN_REVIEW, [])), len(cols.get(board.STAGING, []))
            say(f"Human Review {hr}/{HR_TARGET}, Staging {st}: promoting")
        try:
            with (gatelock.gate_lock('swarm-promoted') if not self.dry else _null()):
                cols = read_columns(self.g['gql'])  # fresh, under the lock
                if needs_promotion(cols):
                    self.fast_path(cols)
                self.slow_path()
        except gatelock.Busy as e:
            say(f"busy: {e}; retrying in {RETRY}s")
            return 'busy'
        return 'promoted'


class _null:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def run(p, poll=POLL, retry=RETRY, sleep=time.sleep):
    wake = {'now': False}
    signal.signal(signal.SIGUSR1, lambda *a: wake.update(now=True))
    say(f"swarm-promoted: polling every {poll}s (SIGUSR1 wakes it)")
    what = 'idle'
    while True:
        try:
            what = p.once(retry=what == 'busy')
        except Exception as e:  # keep the daemon alive; report and retry
            say(f"error: {type(e).__name__}: {e}")
            what = 'busy'
        pause = retry if what == 'busy' else poll
        for _ in range(pause):
            if wake['now']:
                wake['now'] = False
                break
            sleep(1)


def main(argv=None):
    ap = argparse.ArgumentParser(prog='swarm-promoted', description=__doc__.split('\n', 1)[0])
    ap.add_argument('--once', action='store_true')
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args(argv)
    p = Promoter(dry_run=a.dry_run)
    if a.once:
        say('result: ' + p.once())
        return 0
    run(p)


if __name__ == '__main__':
    sys.exit(main())
