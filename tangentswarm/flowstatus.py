"""flow-status: two-slot Human Review, conflict-sorted Staging, ready-next invariant.

    flow-status            report Human Review N/2, the Staging (Pending) queue sorted by
                           conflict count, the ready-next candidate, In Progress, Auto
                           Review, and whether the invariant is OK or VIOLATED
    flow-status --fresh    ignore the report cache

A run took 75-96 s: hr-gate's fetch() built each PR from about 8 REST calls (files,
comments, reviews, check runs), and its board cache lives only 60 s, so every swarmbot
tick paid it for 10 or more PRs. The report needs none of those fields. So:

  1. ONE GraphQL query (filtered to open PRs on the server) reads the board WITH the few PR fields the report uses
     (title, head, draft, body, labels, milestones, status-change times). The PRs are put
     into hr-gate's in-process PR cache, so hr-gate's own queue and fit logic runs with no
     REST call.
  2. ONE git ls-remote reads main and every branch head (the -vN tips too).
  3. The report is kept under a fingerprint of (board rows, the heads it depends on, the
     Staging queue file, the active fast-track). An unchanged tick prints the kept report
     (one GraphQL query, one ls-remote, a few seconds). Only a change runs the git
     merge-tree checks, which hr-gate already keeps per head SHA.

The checks themselves stay in hr-gate (HR_GATE, default ~/bin/hr-gate), loaded with runpy.
Does not move cards or write the queue.
"""
import argparse
import hashlib
import json
import os
import re
import runpy
import subprocess
import sys
from datetime import datetime
from pathlib import Path

GATE = os.environ.get('HR_GATE', os.path.expanduser('~/bin/hr-gate'))
CACHE = Path(os.environ.get('SWARM_FLOW_STATUS_CACHE',
                            str(Path.home() / '.cache' / 'swarm-flow-status.json')))
GIT_TIMEOUT = int(os.environ.get('HR_GATE_SH_TIMEOUT', '120'))
REPO = 'tangentcode/platform'
COLUMNS = ('Human Review', 'Pending', 'In Progress', 'Auto Review')  # hr-gate's internal names

PR_FIELDS = """number state title isDraft headRefName headRefOid body repository{nameWithOwner}
 milestone{title} labels(first:20){nodes{name}}
 closingIssuesReferences(first:10){nodes{number milestone{title} labels(first:20){nodes{name}}}}
 timelineItems(last:30,itemTypes:[PROJECT_V2_ITEM_STATUS_CHANGED_EVENT]){nodes{__typename
   ...on ProjectV2ItemStatusChangedEvent{createdAt status}}}"""

# items(query:) filters on the server: only the ~30 open PRs, not ~330 items in 4 pages
# (0.8 s, not 11 s). Columns are still matched by option id here, never by name in the query.
BOARD_FILTER = 'is:pr is:open'
BOARD_Q = """query($c:String,$f:String!){organization(login:"tangentcode"){projectV2(number:4){
 items(first:100, after:$c, query:$f){
 pageInfo{hasNextPage endCursor}
 nodes{fieldValueByName(name:"Status"){...on ProjectV2ItemFieldSingleSelectValue{name optionId}}
  content{__typename ...on PullRequest{%s}}}}}}}""" % PR_FIELDS


def read_board(gql):
    """(items, prs). items: hr-gate list_board_items rows for every board item. prs: {n: PR
    dict in hr-gate's fetch() shape} for the OPEN platform PRs in COLUMNS. gql must
    normalize status names by option id (hr-gate's gql does: Staging -> Pending)."""
    items, prs, cursor = [], {}, None  # status by option id: hr-gate's gql canon_status
    while True:
        data = gql(BOARD_Q, c=cursor, f=BOARD_FILTER)['organization']['projectV2']['items']
        for node in data['nodes']:
            fv = node.get('fieldValueByName') or {}
            st = {'Staging': 'Pending'}.get(fv.get('name'), fv.get('name'))
            ct = node.get('content') or {}
            kind = ct.get('__typename')
            if kind == 'PullRequest' and (ct.get('repository') or {}).get('nameWithOwner') != REPO:
                continue
            items.append({'status': st, 'number': ct.get('number'), 'state': ct.get('state'), 'typename': kind})
            if kind == 'PullRequest' and ct.get('state') == 'OPEN' and st in COLUMNS:
                pr = dict(ct)
                pr.pop('repository', None)
                prs[ct['number']] = pr
        if not data['pageInfo']['hasNextPage']:
            return items, prs
        cursor = data['pageInfo']['endCursor']


def ls_heads(url, run=subprocess.run):
    """{branch: sha} for every branch on origin, from ONE git ls-remote."""
    r = run(['git', 'ls-remote', '--heads', url], capture_output=True, text=True, timeout=GIT_TIMEOUT)
    if r.returncode:
        raise RuntimeError(f"git ls-remote failed: {(r.stderr or '').strip()[:200]}")
    heads = {}
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].startswith('refs/heads/'):
            heads[parts[1][len('refs/heads/'):]] = parts[0]
    return heads


def relevant_heads(heads, prs):
    """main plus each PR branch and its -vN tips: all the report reads from ls-remote."""
    pats = [re.compile(re.escape(p.get('headRefName') or '') + r'(-v\d+)?') for p in prs.values()]
    return {k: v for k, v in sorted(heads.items()) if k == 'main' or any(p.fullmatch(k) for p in pats)}


def fingerprint(items, prs, heads, queue_text, active):
    """A hash of everything the report depends on. Same inputs, same report."""
    rows = sorted((i['status'] or '', i['number'] or 0, i['state'] or '') for i in items
                  if i.get('typename') == 'PullRequest' and i.get('status') in COLUMNS)
    blob = json.dumps([rows, {str(k): prs[k] for k in sorted(prs)}, relevant_heads(heads, prs),
                       queue_text, active], sort_keys=True)
    return hashlib.sha1(blob.encode()).hexdigest()


def install(g, items, prs, heads):
    """Point hr-gate's readers at the data read above, so its logic makes no REST call.
    A PR not seeded here (a stacked base) still goes through hr-gate's own fetch()."""
    g['_pr_cache'].update(prs)
    g['list_board_items'] = lambda refresh=False: items
    g['remote_sha'] = lambda branch: heads.get(branch)
    g['_remote_heads_cache'] = sorted(heads)
    g['_hr_cache'] = None


def report(g):
    """The report lines (no header). The logic is the old ~/bin/flow-status, unchanged."""
    out = []
    say = out.append

    def title_of(n):
        try:
            return (g['fetch'](n).get('title') or '')[:60]
        except Exception:
            return ''

    target, ip_max, hr_target = g['PENDING_TARGET'], g['IN_PROGRESS_MAX'], g['HR_TARGET']
    with g['ScratchRepo']() as scratch:
        hr = g['list_hr_prs']()
        nhr = len(hr)
        if not hr:
            say(f"Human Review: 0/{hr_target} (empty)")
        else:
            say(f"Human Review: {nhr}/{hr_target}")
            for p in hr:
                try:
                    ok, kind, why, base = g['hr_fit'](p['pr'], hr, scratch, in_hr=True)
                    tag = ('OK ' if ok else 'FAIL ') + why
                except RuntimeError as e:
                    tag = f"ERROR ({e})"
                say(f"  #{p['number']} {p['branch']} {p['head'][:8]}  {title_of(p['number'])}  [{tag}]")

        raw_pending = g['queue_from_board_or_file']()
        pending = g['conflict_sorted_pending'](scratch, raw_pending, hr) if raw_pending else []
        npen = len(pending)
        refill = npen < target
        say(f"Pending: {npen}/{target} (sorted by conflict count)" + ("  refill needed" if refill else ""))
        if not pending:
            say("  (empty)")
        for r in pending:
            c = r.get('conflicts')
            clabel = "independent" if c == 0 else f"{c} conflict(s)"
            say(f"  #{r['pr']} {r['branch']}  tip={r.get('vn_branch') or r['branch']}  "
                f"{clabel}  entered {r['entered']}")

        ip = g['list_column_prs']('In Progress')
        ar = g['list_column_prs']('Auto Review')
        ip_n = len(ip)
        over = f"  over max {ip_max}" if ip_n > ip_max else f"  (max {ip_max})"
        say(f"In Progress: {ip_n}{over}")
        for p in ip:
            draft = " draft" if (p.get('pr') or {}).get('isDraft') else ""
            say(f"  #{p['number']} {p['branch']}{draft}  {title_of(p['number'])}")
        if ar:
            say(f"Auto Review: {len(ar)}")
            for p in ar:
                say(f"  #{p['number']} {p['branch']}  {title_of(p['number'])}")

        ready = g['ready_next_candidate'](scratch, hr, pending)
        if ready:
            c = ready.get('conflicts')
            how = "conflict-free" if c == 0 else f"{c} conflict(s); needs or uses a -vN tip"
            say(f"ready-next: #{ready['pr']} {ready.get('branch')} ({how})")
        else:
            say("ready-next: (none)")

        status, why = g['invariant_status'](hr, pending, ip_n, len(ar), ready)
        say(f"invariant: {status}  {why}")
        if status == 'VIOLATED' and nhr == 0:
            nxt = f"#{ready['pr']}" if ready else "none named"
            say(f"TELL THE REVIEWER: Human Review is empty. Next PR: {nxt}. ETA: unknown until it is gated.")
        if refill:
            say("refill needed")
    return out


def load_cache(path=None):
    try:
        d = json.loads((path or CACHE).read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_cache(d, path=None):
    path = path or CACHE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(d))
    os.replace(tmp, path)


def _read(path):
    try:
        return Path(path).read_text()
    except OSError:
        return ''


def _active():
    from . import board
    try:
        return board.active_fast_track()
    except Exception:
        return None


def run(g, fresh=False, cache_path=None, heads_of=ls_heads, now=None):
    """Lines of the report: the kept one when nothing changed, else a new one."""
    now = now or datetime.now(g['ET'])
    items, prs = read_board(g['gql'])
    heads = heads_of(g['origin_url']())
    fp = fingerprint(items, prs, heads, _read(g['QUEUE_PATH']), _active())
    head = f"flow-status {now:%-I:%M %p} ET"
    kept = load_cache(cache_path)
    if not fresh and kept.get('fp') == fp and isinstance(kept.get('lines'), list):
        return [f"{head}  (no change since {kept.get('at', '?')})"] + kept['lines']
    install(g, items, prs, heads)
    lines = report(g)
    save_cache(dict(fp=fp, at=f"{now:%-I:%M %p}", lines=lines), cache_path)
    return [head] + lines


def main(argv=None):
    ap = argparse.ArgumentParser(prog='flow-status', description=__doc__.split('\n')[0])
    ap.add_argument('--fresh', action='store_true', help='ignore the report cache')
    a = ap.parse_args(argv)
    try:
        g = runpy.run_path(GATE, run_name='flow_status')
        print('\n'.join(run(g, fresh=a.fresh)))
    except (RuntimeError, subprocess.TimeoutExpired) as e:
        print(f"flow-status error: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
