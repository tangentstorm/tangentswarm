"""swarm-advance: ONE step from the end of the line, then exit. No LLM in the path.

The swarmbot loop ran flow-status, then decided by hand which card to move. This does
the first useful step, nearest Human Review first:

  1. promote   Human Review has a free slot and a Staging PR passes the cheap check
               (tangentswarm.promoted fast path: gate pass at this head, open, not a
               draft, not stacked, no later human comment, staging serves the head,
               clean merge with main and each Human Review head): move it.
  2. pull      a pull is running, or Staging is short and Ready has a PR: the swarm-flow
               pull step (one work merge, then hr-gate --apply on the pulled head, which
               checks the image URLs and the static .deploy-sha) or its advance.
  3. minbot    an Auto Review PR is Codex-clean on its exact head (receipt, no findings,
               no open Codex threads), not a draft, merges onto main and has green Linux
               CI (hr-gate conditions 1, 2, 3, 5): add MinBot-passed, post the pass
               comment, and move the card to Ready.

Nothing to do: exit 0 with one summary line.

    swarm-advance [--dry-run]
Exit 0 = a step ran or nothing to do, 75 = busy (queued: the swarm-promoted daemon runs it
under the lock), 2 = error.
"""
import argparse
import json
import os
import re
import subprocess
import sys

from . import board, gatelock, proc

REPO = 'tangentcode/platform'
CODEX = 'chatgpt-codex-connector'
MINBOT = os.environ.get('SWARM_MINBOT', 'minbot')
MINBOT_ROWS = ('1 ', '2 ', '3 ', '5 ')  # not draft, mergeable, Codex clean, Linux CI

CODEX_FIELDS = """number isDraft headRefOid mergeable
 comments(last:40){nodes{author{login} body}}
 reviews(last:30){nodes{author{login} commit{oid} comments{totalCount}}}
 reviewThreads(last:60){nodes{isResolved isOutdated comments(first:1){nodes{author{login}}}}}"""


def _codex(node):
    return ((node or {}).get('author') or {}).get('login', '').startswith(CODEX)


def codex_clean(pr):
    """hr-gate condition 3 on the exact head: a clean receipt (or review-state JSON) for the
    head, no Codex review with findings on the head, and no open Codex thread."""
    head = pr.get('headRefOid') or ''
    if not head:
        return False
    clean = False
    for c in (pr.get('comments') or {}).get('nodes') or []:
        if not _codex(c):
            continue
        body = c.get('body') or ''
        if 'Reviewed commit' in body and head[:8] in body and "Didn't find any major issues" in body:
            clean = True
        m = re.search(r'codex-review-state:v1\s*(\{.*?\})\s*-->', body, re.S)
        if m:
            try:
                st = json.loads(m.group(1)).get('code_review', {})
            except ValueError:
                continue
            if st.get('commit_sha') == head and not st.get('has_any_findings') \
                    and not st.get('has_unresolved_findings'):
                clean = True
    findings = [r for r in (pr.get('reviews') or {}).get('nodes') or []
                if _codex(r) and (r.get('commit') or {}).get('oid') == head
                and ((r.get('comments') or {}).get('totalCount') or 0) > 0]
    open_threads = [t for t in (pr.get('reviewThreads') or {}).get('nodes') or []
                    if not t.get('isResolved') and not t.get('isOutdated')
                    and _codex(((t.get('comments') or {}).get('nodes') or [{}])[0])]
    return clean and not findings and not open_threads


def minbot_order(prs, active=None):
    """Auto Review PRs to try: the active fast-track first, then the lowest number."""
    def key(p):
        ft = active is not None and (p['number'] == active or active in (p.get('closes') or ()))
        return (0 if ft else 1, p['number'])
    return sorted(prs, key=key)


def pass_text(head):
    return (f"Exact-head pass on {head[:10]} (swarm-advance, mechanical): not a draft; merges cleanly "
            "onto main; Codex clean on this head with no open Codex threads; Linux CI green.")


class Advance:
    def __init__(self, g=None, dry_run=False, out=print, minbot=None):
        import runpy
        from .promoted import BIN
        self.g = g if g is not None else runpy.run_path(str(BIN / 'hr-gate'), run_name='swarm_advance')
        self.dry, self.out = dry_run, out
        self.minbot = minbot or self._minbot
        self.why = []  # why each step had nothing to do, for the summary line

    def _minbot(self, *args):
        argv = [MINBOT, *(['--apply'] if not self.dry else []), *map(str, args)]
        r = proc.sh(*argv, check=False, who='swarm-advance')
        if r.returncode:
            raise RuntimeError(f"minbot {' '.join(map(str, args[:2]))} failed: {(r.stderr or r.stdout).strip()[:200]}")
        return r.stdout

    # 1 ---------------------------------------------------------------------
    def promote(self, cols):
        from . import promoted
        hr, st = cols.get(board.HUMAN_REVIEW, []), cols.get(board.STAGING, [])
        if not promoted.needs_promotion(cols):
            self.why.append(f"Human Review {len(hr)}/{promoted.HR_TARGET}"
                            + ('' if len(hr) >= promoted.HR_TARGET else ', Staging empty'))
            return False
        lines = []
        p = promoted.Promoter(g=self.g, dry_run=self.dry)
        old, promoted.say = promoted.say, lines.append
        try:
            moved = p.fast_path(cols)
        finally:
            promoted.say = old
        if moved:
            for line in lines:
                if 'Human Review' in line and ('moved' in line or 'would move' in line):
                    self.out(line)
            return True
        self.why.append(f"Human Review {len(hr)}/{promoted.HR_TARGET} but no Staging PR passes the cheap check ("
                        + '; '.join(f"#{n}: {w}" for n, _, w in p.left)[:300] + ')')
        return False

    # 2 ---------------------------------------------------------------------
    def pull(self, cols):
        from . import flow
        st, ready = cols.get(board.STAGING, []), cols.get(board.READY, [])
        running = flow.load_pull()
        if not running and (len(st) >= flow.STAGING_MIN or not ready):
            self.why.append(f"Staging {len(st)}/{flow.STAGING_MIN}, Ready {len(ready)}")
            return False
        f = flow.Flow(dry_run=self.dry, out=lambda line: None, g=self.g)
        f.step(promote=False)
        for line in f.did:
            self.out('pull: ' + line)
        if f.did:
            return True
        self.why.append('pull: ' + ('; '.join(f.notes)[:300] or 'nothing moved'))
        return False

    # 3 ---------------------------------------------------------------------
    def minbot_pass(self, cols):
        ar = cols.get(board.AUTO_REVIEW, [])
        if not ar:
            self.why.append('Auto Review empty')
            return False
        owner, name = REPO.split('/')
        parts = ' '.join(f'p{p["number"]}: pullRequest(number:{int(p["number"])}){{{CODEX_FIELDS}}}' for p in ar)
        data = self.g['gql'](f'query{{repository(owner:"{owner}",name:"{name}"){{{parts}}}}}')['repository']
        details = {int(k[1:]): v for k, v in data.items() if v}
        skipped = []
        for p in minbot_order(ar, board.active_fast_track()):
            n, d = p['number'], details.get(p['number']) or {}
            if d.get('headRefOid') != p['head']:
                skipped.append(f"#{n} head moved")
                continue
            if d.get('isDraft'):
                skipped.append(f"#{n} draft")
                continue
            if d.get('mergeable') == 'CONFLICTING':  # cheap: no full hr-gate read for it
                skipped.append(f"#{n} conflicts with main (GitHub mergeable)")
                continue
            if not codex_clean(d):
                skipped.append(f"#{n} not Codex-clean on {p['head'][:8]}")
                continue
            why = self.full_check(n)
            if why:
                skipped.append(f"#{n} {why}")
                continue
            self.apply_pass(p)
            return True
        self.why.append('Auto Review: ' + ', '.join(skipped))
        return False

    def full_check(self, n):
        """None if hr-gate conditions 1, 2, 3 and 5 pass on the head, else the failed rows."""
        full = self.g['fetch'](n, refresh=True)
        with self.g['ScratchRepo']() as scratch:
            rows, _ = self.g['evaluate'](full, hr_peers=self.g['list_hr_prs'](), scratch=scratch)
        bad = [f"{name.strip()}: {why[:80]}" for name, ok, why in rows if name.startswith(MINBOT_ROWS) and not ok]
        return '; '.join(bad) if bad else None

    def apply_pass(self, p):
        n, head = p['number'], p['head']
        self.minbot('label', 'add', n, 'MinBot-passed', '--repo', REPO)
        self.minbot('evidence', n, '--repo', REPO, '--title', 'MinBot pass', '--commit', head,
                    '--body', pass_text(head))
        if not self.dry:
            self.g['move'](p['item'], board.READY)
        self.out(f"{'would ' if self.dry else ''}MinBot-pass #{n} at {head[:8]}: label, pass comment, "
                 "Auto Review -> Ready")

    # the step ----------------------------------------------------------------
    def step(self):
        from .promoted import read_columns
        cols = read_columns(self.g['gql'])
        for fn in (self.promote, self.pull, self.minbot_pass):
            if fn(cols):
                return 0
        self.out('nothing to do: ' + '; '.join(self.why))
        return 0


def queue_self(qdir=None):
    """Queue `swarm-advance` (the ~/bin wrapper) for the daemon. Returns a note."""
    from .promoted import BIN
    try:
        gatelock.enqueue([str(BIN / 'swarm-advance')], by=f"pid {os.getpid()}", qdir=qdir)
        return 'queued for swarm-promoted'
    except (OSError, ValueError) as e:
        return f'NOT queued ({e})'


def main(argv=None):
    ap = argparse.ArgumentParser(prog='swarm-advance', description=__doc__.split('\n', 1)[0])
    ap.add_argument('--dry-run', action='store_true', help='print what it would do; change nothing')
    a = ap.parse_args(argv)
    try:
        if a.dry_run:
            return Advance(dry_run=True).step()
        with gatelock.gate_lock('swarm-advance'):
            return Advance().step()
    except gatelock.Busy as e:
        # No "busy, give up": the swarm-promoted daemon owns the queue and runs it under the lock.
        print(f"busy: {e}; " + queue_self())
        return gatelock.EXIT_BUSY
    except (RuntimeError, subprocess.TimeoutExpired, OSError, ValueError) as e:
        print(f"swarm-advance error: {e}", file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
