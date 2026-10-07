"""Swarm status table (port of scialect src/local-status.mts).

For every worker in workers.jsonl: detected agent, state, health and status,
where state/status come from .sci/status-line (or goal.md/result.md mtimes
and the git branch), and health flags a WORKING worker whose screen has not
changed for 10 minutes, or a manager stuck REVIEWING for 5 minutes, as STUCK.
"""
import json
import os
import re
import subprocess
import time

from . import agents
from .workers import load_known_agents, load_workers

HEADERS = ['id', 'agent', 'state', 'health', 'status']
MAX_TABLE_WIDTH = 90
WORKER_STUCK_SECS = 10 * 60
MANAGER_STUCK_SECS = 5 * 60


def _mtime(path):
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


def format_state(goal_mtime, result_mtime):
    if not goal_mtime:
        return 'NO GOAL'
    if result_mtime and result_mtime > goal_mtime:
        return 'DONE'
    return 'BUSY'


def git_status_summary(cwd):
    """' (M:2 ??:1)'-style summary of `git status --porcelain` ('' if clean)."""
    try:
        r = subprocess.run(['git', 'status', '--porcelain'], cwd=cwd, stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, check=False)
    except OSError:
        return ''
    if r.returncode != 0 or not r.stdout:
        return ''
    counts = {}
    for line in r.stdout.splitlines():
        code = line[:2].replace(' ', '')
        counts[code] = counts.get(code, 0) + 1
    out = ''.join(f" {code}:{n}" for code, n in sorted(counts.items()))
    return f" ({out.strip()})" if out else ''


def current_branch(cwd):
    try:
        r = subprocess.run(['git', 'branch', '--show-current'], cwd=cwd, stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, check=False)
        return r.stdout.strip() or '—'
    except OSError:
        return '—'


def parse_status_line(content):
    """Return (STATE, rest) from the first line of a status-line file, or None."""
    first = content.strip().split('\n')[0].strip() if content.strip() else ''
    if not first:
        return None
    m = re.match(r'^([A-Za-z0-9_-]+)[:\s]?(.*)$', first)
    if m:
        return m.group(1).upper(), m.group(2).strip()
    return None, first


def screen_hash(session, window):
    try:
        r = subprocess.run(['tmux', 'capture-pane', '-p', '-t', f"{session}:{window}.0"],
                           stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False)
        if r.returncode == 0 and r.stdout:
            return agents.screen_hash(r.stdout)
    except OSError:
        pass
    return ''


def _update_screen_state(state_path, current_hash, now):
    """Returns True if the screen has been unchanged for WORKER_STUCK_SECS."""
    try:
        if os.path.exists(state_path):
            st = json.loads(open(state_path).read())
            if st.get('hash') == current_hash:
                return now - st.get('timestamp', now) / 1000.0 > WORKER_STUCK_SECS
    except (OSError, ValueError):
        pass
    try:
        with open(state_path, 'w') as f:
            json.dump({'hash': current_hash, 'timestamp': int(now * 1000)}, f)
    except OSError:
        pass
    return False


def collect_swarm_rows(cdir=None):
    """List of [id, agent, state, health, status] rows."""
    rules = load_known_agents(cdir)
    rows = []
    for w in load_workers(cdir):
        try:
            d = w.path
            detected, _ = agents.find_agent_in_window(w.session, w.window, rules)
            branch = current_branch(d)
            sci = os.path.join(d, '.sci')
            state = format_state(_mtime(os.path.join(sci, 'goal.md')), _mtime(os.path.join(sci, 'result.md')))
            suffix = git_status_summary(d)
            status = branch + suffix
            status_path = os.path.join(sci, 'status-line')
            try:
                parsed = parse_status_line(open(status_path, encoding='utf-8').read())
            except OSError:
                parsed = None
            if parsed:
                kw, rest = parsed
                if kw is not None:
                    state = kw
                    status = (rest + suffix) if rest else (kw + suffix)
                else:
                    status = rest + suffix
            health = 'OK'
            now = time.time()
            if w.id == 'mgr':
                if state == 'REVIEWING':
                    m = _mtime(status_path)
                    if m and now - m > MANAGER_STUCK_SECS:
                        health = 'STUCK'
            else:
                state_path = os.path.join(sci, 'screen-state.json')
                if state.startswith('WORKING'):
                    h = screen_hash(w.session, w.window)
                    if h and _update_screen_state(state_path, h, now):
                        health = 'STUCK'
                elif os.path.exists(state_path):
                    try:
                        os.unlink(state_path)
                    except OSError:
                        pass
            rows.append([w.id, detected or 'unknown', state, health, status])
        except Exception as e:  # per-worker failure -> ERROR row
            rows.append([w.id, 'unknown', 'ERROR', 'ERR', str(e)[:80]])
    return rows


def format_swarm_table(rows, max_width=MAX_TABLE_WIDTH):
    """Render rows as an aligned table no wider than max_width (the status
    column absorbs any overflow and is cut with an ellipsis)."""
    all_rows = [HEADERS] + rows
    widths = [max(len(r[i] if i < len(r) else '') for r in all_rows) for i in range(len(HEADERS))]
    sep = ' | '
    last = len(HEADERS) - 1
    budget = max(3, max_width - len(sep) * last - sum(widths[:last]))
    if widths[last] > budget:
        widths[last] = budget

    def fit(cell, w):
        return cell[:max(0, w - 1)] + '…' if len(cell) > w else cell.ljust(w)

    def line(r):
        return sep.join(fit(r[i] if i < len(r) else '', widths[i]) for i in range(len(HEADERS)))

    out = [line(HEADERS), '-+-'.join('-' * w for w in widths)]
    out += [line(r) for r in rows]
    return '\n'.join(out)


def print_swarm_table(rows):
    print(format_swarm_table(rows))


def rows_as_dicts(rows):
    return [dict(zip(HEADERS, r)) for r in rows]


def main(argv=None, cdir=None):
    print_swarm_table(collect_swarm_rows(cdir))
    return 0
