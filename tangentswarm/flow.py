"""swarm-flow: the lean just-in-time Human Review flow (methodology#56), one step at a time.

Columns (matched by option id, see tangentswarm.board):
  Ready         finished PRs (Claude done, Codex clean on head, exact-head MinBot pass).
                They get no merge, no gate and no GitHub check. They wait in order:
                priority milestone, then fewest conflicts, then oldest.
  Staging       3 to 5 fully prepared PRs (internal key "Pending"): merged into work,
                served on staging, CI green, body complete, every gate condition passes.
  Human Review  exactly 2.

One step, under the one global serial lock (a second caller exits 75 "busy"):
  1. Promote: if Human Review has fewer than 2, run `pending-promote --apply`
     (Staging head -> Human Review through hr-gate). Never --fill.
  2. Pull: if Staging has fewer than STAGING_MIN and no pull is running, pick the top
     Ready PR that stacks cleanly on main + the Human Review heads + the Staging heads
     (local merge-tree only). A PR that does not merge onto main goes back to In
     Progress with a merge-main relay. Then merge its head into work (one merge commit,
     pushed as a fast-forward; the push redeploys staging).
  3. Advance a running pull: evaluate hr-gate on the pulled head. All conditions pass:
     `hr-gate --apply N` puts it into Staging. A hard failure sends it back to In
     Progress with the reason. Waiting reasons (CI running, screenshots or the
     where-to-see section still missing) keep the pull until PULL_TIMEOUT, then fail.

Only the pulled PR is merged, deployed or gated. No -vN copies, no re-merge of the queue.

    swarm-flow step [--dry-run]     one step; prints did/would lines and notes
    swarm-flow event NAME < JSON    run a step only for a Status change or a merge
    swarm-flow status               the columns, the Ready order and the running pull
Exit 0 = step done, 75 = busy (another merge/gate step runs), 2 = error.
"""
import argparse
import json
import os
import runpy
import subprocess
import sys
import time
from pathlib import Path

from . import board, gatelock

HOME = Path.home()
BIN = Path(os.environ.get('SWARM_TICK_BIN', str(HOME / 'bin')))
STATE = Path(os.environ.get('SWARM_FLOW_STATE', str(HOME / '.local' / 'state' / 'swarm-flow' / 'pull.json')))
LOG = STATE.with_name('flow.log')
REPO = 'tangentcode/platform'
HR_TARGET = 2
STAGING_MIN = int(os.environ.get('SWARM_STAGING_MIN', '3'))
STAGING_MAX = int(os.environ.get('SWARM_STAGING_MAX', '5'))
PULL_TIMEOUT = int(os.environ.get('SWARM_PULL_TIMEOUT', str(90 * 60)))
WORK = 'work'
# hr-gate rows that mean "wait", not "fail", while a pull is running.
WAIT_ROWS = ('5 ', '6 ', '7a', '7b')
CI_WAIT_WORDS = ('IN_PROGRESS', 'QUEUED', 'PENDING', 'WAITING', 'REQUESTED', 'no Linux GitHub checks')


# ---------------------------------------------------------------------------
# state of the one running pull

def load_pull():
    try:
        d = json.loads(STATE.read_text())
        return d if isinstance(d, dict) and d.get('pr') else None
    except (OSError, ValueError):
        return None


def save_pull(d):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    if d is None:
        STATE.unlink(missing_ok=True)
        return
    tmp = STATE.with_suffix('.tmp')
    tmp.write_text(json.dumps(d, indent=1))
    os.replace(tmp, STATE)


def pulled_pr():
    """PR number of the running pull, or None (read by the hook gate)."""
    d = load_pull()
    return d['pr'] if d else None


def log(line):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, 'a') as f:
        f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')}\t{line}\n")


# ---------------------------------------------------------------------------
# pure helpers

def gate_verdict(rows):
    """('pass', []) | ('wait', reasons) | ('fail', reasons) for hr-gate rows of a pulled PR."""
    fails = [(n, why) for n, ok, why in rows if not ok]
    if not fails:
        return 'pass', []
    hard, wait = [], []
    for n, why in fails:
        if n.startswith(WAIT_ROWS) and not (n.startswith('5 ') and 'FAILURE' in why.upper()):
            if n.startswith('5 ') and not any(w in why for w in CI_WAIT_WORDS):
                hard.append(f"{n.strip()}: {why}")
            else:
                wait.append(f"{n.strip()}: {why}")
        else:
            hard.append(f"{n.strip()}: {why}")
    return ('fail', hard) if hard else ('wait', wait)


def ready_order(ready, conflicts):
    """Ready PRs in pull order: milestone rank, fewest conflicts, then oldest (entry time when
    known, else the lower PR number)."""
    return sorted(ready, key=lambda p: (p['rank'], conflicts.get(p['number'], 0), p.get('entered') or 0, p['number']))


def event_wants_step(name, payload):
    """Cheap filter (no API call): does this webhook event change the flow?"""
    if name == 'projects_v2_item':
        ch = (payload.get('changes') or {}).get('field_value') or {}
        return payload.get('action') in ('edited', 'reordered', 'deleted', 'restored', 'created') and (
            not ch or ch.get('field_node_id') == board.STATUS_FIELD or ch.get('field_name') == 'Status')
    if name == 'pull_request':
        pr = payload.get('pull_request') or {}
        return payload.get('action') == 'closed' and bool(pr.get('merged'))
    return False


# ---------------------------------------------------------------------------
# git in hr-gate's bare scratch repo (no worktree; never touches a Claude checkout)

def git(scratch, *args, check=True):
    r = subprocess.run(['git', '--git-dir', scratch.dir, *args], capture_output=True, text=True, timeout=300)
    if check and r.returncode:
        raise RuntimeError(f"git {' '.join(args[:2])}: {(r.stderr or r.stdout).strip()[:200]}")
    return r


def merge_commit(scratch, a, b, msg):
    """Commit merging b into a, or None on a conflict."""
    r = git(scratch, 'merge-tree', '--write-tree', '--no-messages', a, b, check=False)
    if r.returncode:
        return None
    tree = r.stdout.split()[0]
    return git(scratch, 'commit-tree', tree, '-p', a, '-p', b, '-m', msg).stdout.strip()


def stack_conflict(scratch, main, bases, head):
    """None if head stacks cleanly on main + bases (in order); else the base it conflicts with."""
    cur = main
    for name, sha in bases:
        nxt = merge_commit(scratch, cur, sha, f'stack {name}')
        if nxt is None:
            return f'stack below it ({name})'
        cur = nxt
        if merge_commit(scratch, cur, head, 'probe') is None:
            return name
    return None


# ---------------------------------------------------------------------------
# the step

class Flow:
    def __init__(self, dry_run=False, out=print, g=None, now=time.time):
        self.dry, self.out, self.now = dry_run, out, now
        self.did, self.notes = [], []
        self.g = g if g is not None else runpy.run_path(str(BIN / 'hr-gate'), run_name='swarm_flow')

    def change(self, text):
        self.did.append(('would: ' if self.dry else 'did: ') + text)
        if not self.dry:
            log('did\t' + text)

    def note(self, text):
        self.notes.append(text)

    def tool(self, argv):
        env = dict(os.environ, **{gatelock.HELD_ENV: '1', gatelock.OLD_HELD_ENV: '1'})
        r = subprocess.run([str(a) for a in argv], capture_output=True, text=True, timeout=1800, env=env)
        out = (r.stdout or '') + (r.stderr or '')
        log(f"tool\t{' '.join(str(a) for a in argv)}\trc={r.returncode}\n{out.rstrip()}")
        return r.returncode, out

    # -- board ----------------------------------------------------------------
    def columns(self):
        from . import tick
        prs = tick.board_prs({'gql': self.g['gql'], 'PARKED': self.g.get('PARKED', set())})
        cols = {}
        for n, p in prs.items():
            if p['state'] == 'OPEN' and not p['parked']:
                cols.setdefault(p['col'], []).append(p)
        return prs, cols

    # -- 1. promote -----------------------------------------------------------
    def promote(self, cols):
        hr, staging = cols.get(board.HUMAN_REVIEW, []), cols.get(board.STAGING, [])
        if len(hr) >= HR_TARGET or not staging:
            if len(hr) < HR_TARGET:
                self.note(f"Human Review {len(hr)}/{HR_TARGET} and Staging is empty: nothing to promote")
            return False
        if self.dry:
            self.change(f"pending-promote --apply (Human Review {len(hr)}/{HR_TARGET}, Staging {len(staging)})")
            return True
        rc, out = self.tool([BIN / 'pending-promote', '--apply'])
        moved = [l.strip() for l in out.splitlines() if 'moved #' in l]
        self.change('pending-promote --apply: ' + ('; '.join(moved) if moved else f'rc={rc}, nothing moved'))
        return True

    # -- 2. pull --------------------------------------------------------------
    def pull(self, prs, cols, scratch):
        staging = cols.get(board.STAGING, [])
        if len(staging) >= STAGING_MIN:
            return
        ready = [p for p in cols.get(board.READY, []) if not p['draft']]
        if not ready:
            self.note(f"Staging {len(staging)}/{STAGING_MIN} and Ready is empty: refill needed")
            return
        scratch.ensure_main()
        main = scratch.main
        hr = cols.get(board.HUMAN_REVIEW, [])
        queue = self.staging_order(staging)
        bases = [(f"#{p['number']}", p['head']) for p in hr + queue]
        scratch.ensure(*[sha for _, sha in bases], *[p['head'] for p in ready])
        conflicts = {}
        for p in ready:
            conflicts[p['number']] = sum(not self.g['merge_tree_clean'](scratch.dir, sha, p['head'])
                                         for sha in [main] + [s for _, s in bases])
        for p in ready_order(ready, conflicts):
            n = p['number']
            if not self.g['merge_tree_clean'](scratch.dir, main, p['head']):
                self.fail(p, 'does not merge cleanly onto main: merge origin/main into the branch (no rebase)',
                          relay='merge-main')
                continue
            bad = stack_conflict(scratch, main, bases, p['head'])
            if bad:
                self.note(f"#{n} skipped this step: conflicts with {bad} (Ready order kept)")
                continue
            self.start_pull(p, scratch)
            return
        self.note('no Ready PR stacks cleanly on main + Human Review + Staging: one speculative rebase needed')

    def staging_order(self, staging):
        try:
            order = [r['pr'] for r in self.g['queue_from_board_or_file']()]
        except Exception:
            order = []
        pos = {n: i for i, n in enumerate(order)}
        return sorted(staging, key=lambda p: (pos.get(p['number'], len(pos)), p['number']))

    def start_pull(self, p, scratch):
        n, head = p['number'], p['head']
        git(scratch, 'fetch', '--no-tags', '--quiet', self.g['origin_url'](), f'{WORK}:refs/heads/{WORK}')
        work = git(scratch, 'rev-parse', f'refs/heads/{WORK}').stdout.strip()
        if git(scratch, 'merge-base', '--is-ancestor', head, work, check=False).returncode == 0:
            merged = work
        else:
            merged = merge_commit(scratch, work, head, f"Merge #{n} ({p['branch']}) into {WORK} for Staging\n\n"
                                                        f"swarm-flow pull step, head {head}.")
            if merged is None:
                self.note(f"#{n} conflicts with {WORK}: the work branch needs a manual merge (pull not started)")
                return
            if not self.dry:
                git(scratch, 'push', '--quiet', self.g['origin_url'](), f'{merged}:refs/heads/{WORK}')
        if not self.dry:
            save_pull(dict(pr=n, head=head, branch=p['branch'], item=p['item'], work=merged, started=self.now()))
        self.change(f"#{n} pulled: merged head {head[:8]} into {WORK} ({merged[:8]}); staging redeploys")

    # -- 3. advance -----------------------------------------------------------
    def advance(self, d, prs, scratch):
        n = d['pr']
        p = prs.get(n)
        if p is None or p['state'] != 'OPEN':
            self.change(f"#{n} pull dropped: the PR is no longer open on the board")
            if not self.dry:
                save_pull(None)
            return
        if p['col'] in (board.STAGING, board.HUMAN_REVIEW):
            self.change(f"#{n} pull finished: the card is in {p['col']}")
            if not self.dry:
                save_pull(None)
            return
        if p['head'] != d['head']:
            self.fail(p, f"head moved during the pull ({d['head'][:8]} -> {p['head'][:8]}): needs Codex and MinBot again")
            return
        full = self.g['fetch'](n, refresh=True)
        rows, _ = self.g['evaluate'](full, hr_peers=self.g['list_hr_prs'](refresh=True), scratch=scratch)
        verdict, why = gate_verdict(rows)
        age = int(self.now() - d['started'])
        if verdict == 'wait' and age > PULL_TIMEOUT:
            verdict, why = 'fail', [f"still waiting after {age // 60} min: " + '; '.join(why)]
        if verdict == 'wait':
            self.note(f"#{n} pull waiting ({age // 60} min): " + '; '.join(w[:120] for w in why))
            return
        if verdict == 'fail':
            self.fail(p, '; '.join(why))
            return
        if self.dry:
            self.change(f"#{n} hr-gate --apply {n} (every condition passes)")
            return
        rc, out = self.tool([BIN / 'hr-gate', '--apply', str(n)])
        msg = [l.strip() for l in out.splitlines() if l.strip().startswith(('moved', 'refused', 'stayed', 'stays'))]
        self.change(f"#{n} hr-gate --apply: " + (msg[-1] if msg else f'rc={rc}'))
        if rc == 0 or any(m.startswith('moved') for m in msg):
            save_pull(None)

    def fail(self, p, reason, relay=None):
        n = p['number']
        if not self.dry:
            self.g['move'](p['item'], board.IN_PROGRESS)
            path = Path(f"/tmp/relay-{n}-{relay or 'gate'}.md")
            path.write_text(f"Coordinator: #{n} left the Staging pull: {reason}. Fix the root cause, push, and "
                            "report the head commit and test counts.\n")
            running = load_pull()
            if running and running.get('pr') == n:
                save_pull(None)
        self.change(f"#{n} -> In Progress: {reason[:200]}")

    # -- the step ---------------------------------------------------------------
    def step(self):
        prs, cols = self.columns()
        hr, staging = cols.get(board.HUMAN_REVIEW, []), cols.get(board.STAGING, [])
        if len(hr) > HR_TARGET:
            self.note(f"Human Review holds {len(hr)} PRs (more than {HR_TARGET}): judgment needed")
        if len(staging) > STAGING_MAX:
            self.note(f"Staging holds {len(staging)} PRs (more than {STAGING_MAX})")
        if self.promote(cols):
            prs, cols = self.columns() if not self.dry else (prs, cols)
        with self.g['ScratchRepo']() as scratch:
            d = load_pull()
            if d:
                self.advance(d, prs, scratch)
            running = d if self.dry else load_pull()
            if not running:
                self.pull(prs, cols, scratch)
        for line in self.did + self.notes:
            self.out(line)
        return 0

    def status(self):
        prs, cols = self.columns()
        for key in (board.HUMAN_REVIEW, board.STAGING, board.READY):
            ps = cols.get(key, [])
            label = 'Staging' if key == board.STAGING else key
            self.out(f"{label} {len(ps)}: " + (', '.join(f"#{p['number']}" for p in ps) or '-'))
        d = load_pull()
        self.out(f"pull: #{d['pr']} head {d['head'][:8]} since {int((self.now() - d['started']) // 60)} min"
                 if d else 'pull: none')
        return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog='swarm-flow', description=__doc__.split('\n', 1)[0])
    ap.add_argument('cmd', choices=['step', 'event', 'status'])
    ap.add_argument('name', nargs='?', help='webhook event name (event)')
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args(argv)
    try:
        if a.cmd == 'status':
            return Flow().status()
        if a.cmd == 'event':
            payload = json.load(sys.stdin)
            if not event_wants_step(a.name or '', payload):
                print(f"skip: {a.name} {payload.get('action')} does not change the flow")
                return 0
        if a.dry_run:
            return Flow(dry_run=True).step()
        with gatelock.gate_lock('swarm-flow step'):
            return Flow().step()
    except gatelock.Busy as e:
        print(f"busy: {e}")
        return gatelock.EXIT_BUSY
    except (RuntimeError, subprocess.TimeoutExpired, OSError, ValueError) as e:
        print(f"swarm-flow error: {e}", file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
