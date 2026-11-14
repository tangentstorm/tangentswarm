"""swarm-mainmerge: after main moves, recheck ONLY "merges cleanly with main" per Staging PR.

A merge to main used to force full rechecks (hr-gate --sweep --demote, pending-promote),
each several minutes under the serial lock. Nothing else a Staging PR passed at entry
depends on main. So on a new main SHA, each Staging PR gets one local merge-tree probe
(no API call; cached per (main, head)). A PR that conflicts goes to In Progress and gets
a merge-main relay (merge commit, no rebase). A clean PR is left alone.

The relay is written to /tmp/relay-<PR>-mergemain-<HHMM>.md. It is sent at once only
if the PR's Claude pane shows an empty prompt (tell_agent's probe); otherwise it stays a
held relay for the usual retry. The swarm-promoted daemon calls recheck() when it sees a
new main SHA (git ls-remote, every poll).
"""
import json
import os
import subprocess
import time
from pathlib import Path

from . import board

CACHE = Path(os.environ.get('SWARM_MAINMERGE_CACHE', str(Path.home() / '.cache' / 'swarm-mainmerge.json')))
RELAY_DIR = Path(os.environ.get('SWARM_RELAY_DIR', '/tmp'))
KEEP = 500  # cache entries kept


def load_cache(path=None):
    path = path or CACHE
    try:
        d = json.loads(path.read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_cache(d, path=None):
    path = path or CACHE
    path.parent.mkdir(parents=True, exist_ok=True)
    keep = dict(list(d.items())[-KEEP:])
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(keep))
    os.replace(tmp, path)


def conflicts(staging, main, clean, cache):
    """[pr] in `staging` whose head does not merge cleanly onto main. clean(main, head) -> bool
    runs only on a cache miss; cache maps 'main:head' to the result."""
    out = []
    for p in staging:
        key = f"{main}:{p['head']}"
        if key not in cache:
            cache[key] = bool(clean(main, p['head']))
        if not cache[key]:
            out.append(p)
    return out


def relay_text(n, branch, head, main):
    return (f"Coordinator: main moved to {main[:8]}, and PR #{n} head {branch} {head[:8]} now CONFLICTS with main. "
            f"The card is In Progress. On branch {branch} run git fetch origin, then git merge origin/main "
            f"(a merge commit; NO rebase, NO force-push). Resolve the conflicts, run only the directly relevant "
            f"tests through ~/bin/test-slot, and push {branch}. Do not post on GitHub. Reply with the new head "
            f"SHA and the tests you ran.\n")


def write_relay(n, text, now=None):
    stamp = time.strftime('%H%M', time.localtime(now))
    path = RELAY_DIR / f"relay-{n}-mergemain-{stamp}.md"
    path.write_text(text)
    return path


def find_pane(pr, list_panes=None):
    """The tmux pane id of the PR's Claude, or None. pr needs number and branch."""
    from . import tick
    if list_panes is None:
        r = subprocess.run(['tmux', 'list-panes', '-a', '-F',
                            '#{pane_id}\t#{window_name}\t#{pane_current_command}\t#{pane_current_path}'],
                           capture_output=True, text=True, timeout=30)
        rows = r.stdout.splitlines() if r.returncode == 0 else []
    else:
        rows = list_panes()
    for row in rows:
        pid, window, cmd, path = (row.split('\t') + ['', '', '', ''])[:4]
        if tick.window_matches_pr(window, path, pr) and tick.is_claude(pid, cmd):
            return pid
    return None


def deliver(pr, text, find=find_pane, tell=None):
    """Send the relay if the PR's Claude has an empty prompt. Returns 'sent' or 'held: why'."""
    if tell is None:
        from .agents import tell_agent as tell
    pane = find(pr)
    if pane is None:
        return 'held: no live Claude pane'
    try:
        tell(pane, text.strip())
    except Exception as e:  # AgentNotReady and tmux errors: keep the file for the retry
        return f'held: {e}'
    return 'sent'


def recheck(staging, main, clean, move, say, details=None, dry=False, cache_path=None,
            send=deliver, now=None):
    """Move each Staging PR that conflicts with the new main to In Progress and relay a
    merge-main. Returns the PR numbers that conflict."""
    cache = load_cache(cache_path)
    bad = conflicts(staging, main, clean, cache)
    save_cache(cache, cache_path)
    for p in bad:
        n = p['number']
        branch = ((details or {}).get(n) or {}).get('headRefName') or p.get('branch') or f'PR #{n} branch'
        if dry:
            say(f"would move #{n} Staging -> In Progress: head {p['head'][:8]} conflicts with main {main[:8]}")
            continue
        move(p['item'], board.IN_PROGRESS)
        text = relay_text(n, branch, p['head'], main)
        path = write_relay(n, text, now)
        how = send(dict(number=n, branch=branch), text)
        say(f"moved #{n} Staging -> In Progress: conflicts with main {main[:8]}; merge-main relay {path.name} {how}")
    if staging and not bad:
        say(f"main {main[:8]}: every Staging PR still merges cleanly ({len(staging)})")
    return [p['number'] for p in bad]
