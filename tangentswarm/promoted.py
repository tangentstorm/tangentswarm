"""swarm-promoted: refill Human Review within seconds, with no LLM in the path.

A systemd user service. Every POLL seconds (or at once on SIGUSR1, which a webhook
receiver may send) it reads the Project 4 Status column of every PR in one lean
GraphQL query (through the minbot shim, so on the app quota). An OPEN PR counts; a
merged PR whose card still says Human Review does not. When Human Review holds fewer
than 2 and Staging is not empty, it takes the one serial lock (non-blocking; if busy
it retries every RETRY seconds) and:

  1. Fast path, a CHEAP check only (the full checks ran when the PR entered Staging).
     Staging PRs go fast-track first, then in hr-gate's queue order. One may move to
     Human Review when:
       - hr-gate's latest apply line for its current head is PASS or PENDING with no
         failed condition (the head did not change since the gate),
       - it is open, not a draft, and not stacked (base main, no stacked banner),
       - no person (a User, not a bot or app) commented or reviewed after that gate,
       - for a PR that touches web/, app/ or api/: the static .deploy-sha contains the
         head (hr-gate condition 6, strict on entry),
       - it merges cleanly with main, and with main plus each Human Review head
         (local merge-tree, no API call).
     The reads are one GraphQL query for the Staging PRs. The card moves and an hr-gate
     PASS line is written. This repeats until Human Review is full.
  2. When main moved (git ls-remote each poll, no API quota): recheck ONLY "merges
     cleanly with main" for each Staging PR left (tangentswarm.mainmerge). A conflict
     goes to In Progress with a merge-main relay; a clean PR is left alone.
  3. Queued gate requests: a caller that found the serial lock busy queued its command
     (tangentswarm.gatelock) instead of giving up; the daemon runs each one under the lock.
  4. Only if Human Review is still short and a Staging PR is left (stacked, or no gate
     pass): pending-promote --apply, still under the lock. It runs ONCE per leftover
     (PR, head), then again only after BACKSTOP seconds; a blocked head is re-gated by its
     own event (the CI hook), not by a poll. 
  5. With SWARM_PROMOTE_PULL=1 (off by default): the swarm-flow pull and its advance,
     with no promote step. When Staging is short and Ready has a PR, the top Ready PR
     that stacks cleanly is merged into work ONCE (the push redeploys staging) and gated
     into Staging by hr-gate --apply.

    swarm-promoted                  run the daemon
    swarm-promoted --once [--dry-run]   one check (and promotion) and exit
"""
import argparse
import os
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

from . import board, gatechecks, gatelock

HOME = Path.home()
BIN = Path(os.environ.get('SWARM_TICK_BIN', str(HOME / 'bin')))
POLL = int(os.environ.get('SWARM_PROMOTE_POLL', '30'))
RETRY = int(os.environ.get('SWARM_PROMOTE_RETRY', '5'))
PULL = os.environ.get('SWARM_PROMOTE_PULL') == '1'  # also run the Staging pull
REPO = 'tangentcode/platform'
BACKSTOP = int(os.environ.get('SWARM_PROMOTE_BACKSTOP', '900'))  # retry a leftover head at most this often
HR_TARGET = int(os.environ.get('SWARM_PROMOTE_HR_TARGET', '2'))  # only raise it for a dry-run proof

LEAN_Q = """query($c:String){organization(login:"tangentcode"){projectV2(number:4){items(first:100, after:$c){
 pageInfo{hasNextPage endCursor}
 nodes{id fieldValueByName(name:"Status"){...on ProjectV2ItemFieldSingleSelectValue{name optionId}}
  content{__typename ...on PullRequest{number state headRefOid repository{nameWithOwner}
   labels(first:20){nodes{name}} closingIssuesReferences(first:10){nodes{number labels(first:20){nodes{name}}}}}
   ...on Issue{number state repository{nameWithOwner} labels(first:20){nodes{name}}}}}}}}}"""


def say(line):
    print(time.strftime('%H:%M:%S ') + line, flush=True)


def _names(node):
    return [l['name'] for l in ((node or {}).get('labels') or {}).get('nodes') or []]


def read_board(gql):
    """({column key: [{number, head, item}]} for OPEN platform PRs by option id,
    [fast-track items for board.update_active])."""
    cols, ft, cursor = {}, [], None
    while True:
        data = gql(LEAN_Q, c=cursor)['organization']['projectV2']['items']
        for it in data['nodes']:
            c = it.get('content') or {}
            kind = c.get('__typename')
            if kind not in ('PullRequest', 'Issue') or (c.get('repository') or {}).get('nameWithOwner') != REPO:
                continue
            col = board.column(it.get('fieldValueByName'))
            closes = [dict(number=i['number'], labels=_names(i))
                      for i in ((c.get('closingIssuesReferences') or {}).get('nodes') or [])]
            ft.append(dict(number=c['number'], kind=kind, state=c.get('state'), col=col,
                           labels=_names(c), closes=closes))
            if kind == 'PullRequest' and c.get('state') == 'OPEN':
                cols.setdefault(col, []).append(dict(number=c['number'], head=c['headRefOid'], item=it['id'],
                                                     closes=[i['number'] for i in closes]))
        if not data['pageInfo']['hasNextPage']:
            return cols, ft
        cursor = data['pageInfo']['endCursor']


def read_columns(gql, ft_state=None):
    """Columns as read_board, and keep the ONE active fast-track ticket current (board.FT_STATE)."""
    cols, ft = read_board(gql)
    try:
        act, prev = board.update_active(ft, ft_state)
        if act != prev:
            say(f"active fast-track: {'#' + str(act) if act else 'none'} (was {'#' + str(prev) if prev else 'none'})")
    except OSError as e:
        say(f"fast-track state not saved: {e}")
    return cols


def needs_pull(cols, running):
    """With SWARM_PROMOTE_PULL=1: a pull runs, or Staging is short and Ready has a PR."""
    from . import flow
    if not PULL:
        return False
    return bool(running) or (len(cols.get(board.STAGING, [])) < flow.STAGING_MIN and bool(cols.get(board.READY)))


def needs_promotion(cols):
    return len(cols.get(board.HUMAN_REVIEW, [])) < HR_TARGET and bool(cols.get(board.STAGING))


def _when(text):
    try:
        t = datetime.fromisoformat(text.replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else None


def gate_times(log_text):
    """{(#n, head8): time} for heads whose LATEST hr-gate apply line is PASS or PENDING with
    no failed condition. A later BLOCKED or RELAY line at the same head cancels the pass."""
    last = {}
    for line in log_text.splitlines():
        p = line.split('\t')
        if len(p) > 5 and p[1] == 'apply':
            last[(p[2], p[3])] = (p[4] in ('PASS', 'PENDING') and p[5] == 'fail=-', _when(p[0]))
    return {k: t for k, (ok, t) in last.items() if ok and t is not None}


def gated_heads(log_text):
    return set(gate_times(log_text))


DETAIL_FIELDS = """number state headRefOid headRefName isDraft baseRefName body files(first:100){nodes{path}}
 labels(first:20){nodes{name}} closingIssuesReferences(first:10){nodes{number labels(first:20){nodes{name}}}}
 comments(last:30){nodes{createdAt author{__typename login}}}
 reviews(last:30){nodes{submittedAt author{__typename login}}}"""


def read_details(gql, numbers):
    """{n: PR fields} for a few PRs in ONE GraphQL query (no REST)."""
    if not numbers:
        return {}
    owner, name = REPO.split('/')
    parts = ' '.join(f'p{n}: pullRequest(number:{int(n)}){{{DETAIL_FIELDS}}}' for n in numbers)
    data = gql(f'query{{repository(owner:"{owner}",name:"{name}"){{{parts}}}}}')['repository']
    return {int(k[1:]): v for k, v in data.items() if v}


def human_after(pr, since):
    """Logins of people (not bots or apps) who commented or reviewed after `since`."""
    who = []
    for kind, key in (('comments', 'createdAt'), ('reviews', 'submittedAt')):
        for c in (pr.get(kind) or {}).get('nodes') or []:
            a, t = c.get('author') or {}, _when(c.get(key) or '')
            if a.get('__typename') == 'User' and t is not None and t > since:
                who.append(a.get('login') or '?')
    return who


def pr_labels(pr):
    names = [l['name'] for l in (pr.get('labels') or {}).get('nodes') or []]
    for iss in (pr.get('closingIssuesReferences') or {}).get('nodes') or []:
        names += [l['name'] for l in (iss.get('labels') or {}).get('nodes') or []]
    return names


def staging_order(staging, details, queue, active=None):
    """The ONE active fast-track first, then hr-gate's queue file order, then the lowest number."""
    pos = {r['pr']: i for i, r in enumerate(queue)}
    def key(p):
        closes = p.get('closes')
        if closes is None:
            closes = [i.get('number') for i in ((details.get(p['number'], {}).get('closingIssuesReferences') or {})
                                                 .get('nodes') or [])]
        ft = board.is_active_fast_track(p['number'], closes, active)
        return (0 if ft else 1, pos.get(p['number'], len(pos)), p['number'])
    return sorted(staging, key=key)


def staging_head(cols, queue):
    """The Staging PR that goes next by queue order alone (no labels read)."""
    order = staging_order(cols.get(board.STAGING, []), {}, queue)
    return order[0] if order else None


def merge_problem(merge_commit, scratch, main, hr, head):
    """None if head merges cleanly onto main, and onto main plus each Human Review head
    (pairwise, like hr-gate's condition 9). Else a short reason. Local merge-tree only."""
    if merge_commit(scratch, main, head, 'probe main') is None:
        return 'conflicts with main'
    for n, sha in hr:
        base = merge_commit(scratch, main, sha, f'main + #{n}')
        if base is not None and merge_commit(scratch, base, head, 'probe') is None:
            return f'conflicts with #{n} in Human Review'
    return None


class Promoter:
    def __init__(self, g=None, dry_run=False):
        import runpy
        self.g = g if g is not None else runpy.run_path(str(BIN / 'hr-gate'), run_name='swarm_promoted')
        self.dry = dry_run
        self.left = []    # [(n, head, why)] Staging PRs the last fast path did not move
        self.tried = {}   # {(n, head): time} the slow path last ran for this leftover head
        self.last_main = None  # main SHA the last main-merge recheck ran for

    def main_sha(self):
        """origin main's SHA via git ls-remote (no API quota), or None on an error."""
        try:
            r = self.g['sh']('git', 'ls-remote', self.g['origin_url'](), 'refs/heads/main', check=False)
            sha = (r.stdout or '').split()[0] if r.returncode == 0 and r.stdout.strip() else ''
            return sha if len(sha) == 40 else None
        except Exception:
            return None

    def drain(self):
        """Run the queued gate requests (tangentswarm.gatelock queue) under our lock."""
        if self.dry:
            for _, req in gatelock.queued():
                say(f"would run queued {' '.join(req.get('argv') or [])}")
            return []
        return gatelock.drain(gatelock.run_held, say)

    def main_pass(self, cols, moved, main):
        """Main moved: recheck only "merges cleanly with main" for the Staging PRs left."""
        from . import flow, mainmerge
        staging = [p for p in cols.get(board.STAGING, []) if p['number'] not in moved]
        if not staging:
            return []
        details = read_details(self.g['gql'], [p['number'] for p in staging])
        with self.g['ScratchRepo']() as scratch:
            def clean(m, head):
                scratch.ensure(m, head)
                return flow.merge_commit(scratch, m, head, 'probe main') is not None
            return mainmerge.recheck(staging, main, clean, self.g['move'], say, details=details, dry=self.dry)

    def slow_due(self, now=None):
        """True if a leftover Staging head has not had the slow path yet, or not for BACKSTOP
        seconds. A blocked head is re-gated by its own event (the CI hook), so re-running the
        4-minute pending-promote for it on every poll only holds the lock against that event."""
        now = time.time() if now is None else now
        return any(now - self.tried.get((n, h), -BACKSTOP) >= BACKSTOP for n, h, _ in self.left)

    def mark_tried(self, now=None):
        now = time.time() if now is None else now
        for n, h, _ in self.left:
            self.tried[(n, h)] = now

    def log_text(self):
        try:
            return Path(self.g['LOG']).read_text()
        except OSError:
            return ''

    def queue(self):
        try:
            return self.g['read_queue_file']()
        except Exception:
            return []

    def cheap_check(self, p, d, gated, merge):
        """None if PR p may move to Human Review now, else the reason it may not.
        The full checks ran when it entered Staging; here only what can change since."""
        n, head = p['number'], p['head']
        t = gated.get((f'#{n}', head[:8]))
        if t is None:
            return f'head {head[:8]} has no gate pass'
        if not d or d.get('state') != 'OPEN':
            return 'not open'
        if d['headRefOid'] != head:
            return f"head moved to {d['headRefOid'][:8]}"
        if d.get('isDraft'):
            return 'is a draft'
        if d.get('baseRefName') != 'main' or self.g['stacked_base'](d.get('body') or ''):
            return 'is stacked (pending-promote decides)'
        who = human_after(d, t)
        if who:
            return f"human comment after the gate ({', '.join(sorted(set(who)))})"
        return self.served_problem(d, head) or merge(head)

    def served_problem(self, d, head):
        """None if staging serves the head, else why not. Only for a PR that touches served
        files (web/, app/, api/): hr-gate condition 6, strict on entry (platform #479 entered
        Human Review before staging served its head). Local reads only: the .deploy-sha file
        and git merge-base."""
        paths = [f['path'] for f in ((d.get('files') or {}).get('nodes') or []) if f.get('path')]
        if not any(gatechecks.SERVED_RE.search(x) for x in paths):
            return None
        host, sha, _, _, err = self.g['served'](self.g['site_for'](d, None))
        ok, why = gatechecks.check_served(paths, True, sha, lambda s: self.g['contains'](head, s), err)
        return None if ok else f"{host}: {why}"

    def fast_path(self, cols):
        """Fill Human Review from Staging with the cheap check only. Returns the moved PRs."""
        from . import flow
        hr = list(cols.get(board.HUMAN_REVIEW, []))
        staging = cols.get(board.STAGING, [])
        if len(hr) >= HR_TARGET or not staging:
            return []
        details = read_details(self.g['gql'], [p['number'] for p in staging])
        gated = gate_times(self.log_text())
        moved, self.left = [], []
        with self.g['ScratchRepo']() as scratch:
            main = None

            def merge(head):
                nonlocal main
                if main is None:
                    main = scratch.ensure_main()
                scratch.ensure(head, *[q['head'] for q in hr])
                return merge_problem(flow.merge_commit, scratch, main,
                                     [(q['number'], q['head']) for q in hr], head)

            for p in staging_order(staging, details, self.queue(), board.active_fast_track()):
                if len(hr) >= HR_TARGET:
                    break
                n, head = p['number'], p['head']
                why = self.cheap_check(p, details.get(n), gated, merge)
                if why:
                    say(f"#{n} stays in Staging: {why}")
                    self.left.append((n, head, why))
                    continue
                if self.dry:
                    say(f"would move #{n} Staging -> Human Review (cheap check passed at {head[:8]})")
                else:
                    self.g['move'](p['item'], board.HUMAN_REVIEW)
                    self.g['log']('apply', n, head[:8], 'PASS', [])
                    say(f"moved #{n} Staging -> Human Review (cheap check passed at {head[:8]})")
                hr.append(p)
                moved.append(n)
        return moved

    def slow_path(self):
        """After the fast move: pending-promote if Human Review is still short."""
        from . import flow
        f = flow.Flow(dry_run=self.dry, out=lambda line: None, g=self.g)
        _, cols = f.columns()
        f.promote(cols)
        for line in f.did + f.notes:
            say('flow: ' + line)

    def pull_path(self):
        """SWARM_PROMOTE_PULL=1: the swarm-flow pull and its advance, with no promote step.
        The work merge happens ONCE, when the pulled PR enters Staging."""
        from . import flow
        f = flow.Flow(dry_run=self.dry, out=lambda line: None, g=self.g)
        f.step(promote=False)
        for line in f.did + f.notes:
            say('flow: ' + line)

    def once(self, retry=False):
        """One check. Returns 'idle', 'busy' or 'promoted'. A retry after busy goes straight
        to the lock (the read under the lock is fresh), so a long busy spell costs no reads."""
        main = self.main_sha()
        main_moved = main is not None and main != self.last_main
        waiting = gatelock.queued()
        if not retry:
            cols = read_columns(self.g['gql'])
            from . import flow
            pull_due = needs_pull(cols, flow.load_pull())
            if not needs_promotion(cols) and not main_moved and not waiting and not pull_due:
                return 'idle'
            if waiting:
                say(f"{len(waiting)} queued gate request(s): draining")
            hr, st = len(cols.get(board.HUMAN_REVIEW, [])), len(cols.get(board.STAGING, []))
            if needs_promotion(cols):
                say(f"Human Review {hr}/{HR_TARGET}, Staging {st}: promoting")
            if main_moved:
                say(f"main is at {main[:8]} (was {(self.last_main or 'unknown')[:8]}): Staging merge recheck")
        try:
            with (gatelock.gate_lock('swarm-promoted') if not self.dry else _null()):
                cols = read_columns(self.g['gql'])  # fresh, under the lock
                moved = self.fast_path(cols) if needs_promotion(cols) else []
                drained = self.drain()
                if drained:  # a queued run may have moved cards: read again before the rest
                    cols, moved = read_columns(self.g['gql']), []  # the new read already has our moves
                if main_moved:
                    self.main_pass(cols, moved, main)
                    self.last_main = main
                hr = len(cols.get(board.HUMAN_REVIEW, [])) + len(moved)
                if hr < HR_TARGET and any(a[0].endswith('pending-promote') for a, _ in drained):
                    self.mark_tried()  # a queued pending-promote just ran: that was the slow path
                elif hr < HR_TARGET and self.slow_due():
                    self.mark_tried()
                    self.slow_path()
                elif hr < HR_TARGET and self.left:
                    say('slow path already ran for ' + ', '.join(f'#{n} at {h[:8]}' for n, h, _ in self.left)
                        + f'; waiting for a new head or gate event (backstop {BACKSTOP}s)')
                if PULL:
                    self.pull_path()
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
