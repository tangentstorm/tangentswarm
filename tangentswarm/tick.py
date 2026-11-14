"""swarm-tick: one deterministic pass of the tangentcode/platform swarm loop (no LLM).

A routine runs it every 15 minutes, so the board keeps moving without a person or a
bot holding it together by hand. Each pass:

  1. Reads Project 4 and every pane in the tmux session `agents`. Each pane that sits
     in a platform worktree gets one class:
       working     Claude shows "esc to interrupt" (stuck if its screen is unchanged
                   for STUCK_MINUTES)
       finished    Claude is idle at an empty prompt, the worktree is clean and the
                   local HEAD is the pushed PR head
       idle-dirty  Claude is idle, but the worktree is dirty or HEAD is not pushed
       asking      Claude is idle and its last lines end in a question
       input       Claude is idle with text typed in its prompt that was not sent (this pass submits it)
       menu        Claude shows a numbered choice menu (a permission prompt and so on)
       exited      a shell where Claude ran, while the PR is still open
  1b. For each open PR in In Progress or Auto Review whose body has no brief section,
     writes the coordinator's brief file into the body (tangentswarm.brief). A PR with
     no brief file is reported for judgment.
  2. For an In Progress PR whose Claude is finished, with no open Codex findings and a
     head that merges cleanly onto origin/main: marks the PR ready, moves the card to
     Auto Review and posts "@codex review" (once per head).
  3. (Retired by methodology#56: Ready PRs get no gate. Only the flow step's one pulled
     PR is gated.)
  3b. Starts at most one held launch (swarm-launch queue) when the worker slice has
     room, priority milestone first, then the oldest hold. Memory limits are read live.
     Memory pressure that lasts PRESSURE_MINUTES or more is reported, with the numbers
     and the held launches.
  4. Runs `active-gate --sweep --demote` and `active-gate --promote-missing`.
  5. Runs one swarm-flow step (tangentswarm.flow): promote Staging -> Human Review when
     Human Review has fewer than 2, and pull one Ready PR into Staging.
  6. Closes every tmux window in session `agents` that belongs to a MERGED or CLOSED
     PR (the numbered name, a legacy name without the number, or a pane in that
     branch's worktree), except a window whose foreground command is claude or node,
     or whose worktree has uncommitted or unpushed changes. Then prints one "did: ..."
     line per change, then one line per item that needs judgment:
     "#PR %pane window: reason". With nothing done and nothing to judge it prints nothing.

--dry-run changes nothing on GitHub, the board or tmux, and writes no state file. It
prints "would: ..." for steps 2-6 instead (pending-promote --dry-run may still write
its /tmp relay files).
--verbose also prints every pane's class and the full output of each tool.

Hard rules, enforced here: it never merges, never deploys, never types new text into
a pane, never types over a menu, never kills a process, never closes a window whose
foreground command is claude or node, and never reads a credential file. When a Claude
is idle with text already typed in its prompt (a relay that was not sent), it submits
that text by pressing Enter. GitHub access goes through `gh`. Card moves
into Human Review go only through hr-gate.
Every change and tool output is appended to ~/.local/state/swarm-tick/tick.log.
Exit 0 = pass finished (items may still need judgment), 2 = tool error.
"""
import argparse
import fcntl
import hashlib
import json
import os
import re
import runpy
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import board, brief, flow, gatelock, launches

ET = ZoneInfo('America/New_York')
HOME = Path.home()
BIN = Path(os.environ.get('SWARM_TICK_BIN', str(HOME / 'bin')))
STATE_DIR = Path(os.environ.get('SWARM_TICK_STATE', str(HOME / '.local' / 'state' / 'swarm-tick')))
LOCK = HOME / '.cache' / 'swarm-tick.lock'
MN_STATE = HOME / '.local' / 'state' / 'mn-workflow' / 'state.json'
PARK_REGISTRY = HOME / 'var' / 'swarm-parked.tsv'
PLATFORM_GIT = HOME / 'ver' / 'platform'
REPO = 'tangentcode/platform'
SESSION = os.environ.get('SWARM_TICK_SESSION', 'agents')
CODEX = 'chatgpt-codex-connector'
PRIORITY_MILESTONES = {m.strip().lower() for m in
                       os.environ.get('SWARM_PRIORITY_MILESTONES', 'ptw 15').split(',') if m.strip()}
WORKING_COLS = ('In Progress', 'Auto Review')
LATER_COLS = ('Pending', 'Human Review', 'Done', 'Canceled')
EXTRA_PARKED = {202, 344, 350}
SHELLS = {'bash', 'sh', 'zsh', 'fish', 'dash'}
STUCK_MINUTES = 20
PRESSURE_MINUTES = int(os.environ.get('SWARM_PRESSURE_MINUTES', '10'))
SETTLE = 1.5

# ---------------------------------------------------------------------------
# pure helpers (unit-tested without tmux or GitHub)

BUSY_RE = re.compile(r'esc to interrupt|ctrl\+c to interrupt', re.I)
MENU_RE = re.compile(r'^\s*[❯>]\s*1\.\s|Enter to confirm|Esc to cancel|'
                     r'^\s*Do you want to (?:proceed|make this edit|create|run|allow)', re.M | re.I)
WINDOW_PR_RE = re.compile(r'^[a-z/]+-(\d+)-')
DIGITS_RE = re.compile(r'\d+')
SCAFFOLD_RE = re.compile(r'^Start #\d+\s*$')


def _bars(lines):
    return [i for i, line in enumerate(lines) if '─' in line and len(line.replace('─', '').strip()) < 15]


def _above_input(screen):
    lines = screen.rstrip('\n').split('\n')
    bars = _bars(lines)
    top = bars[-2] if len(bars) >= 2 else len(lines)
    body = [l.strip() for l in lines[:top] if l.strip()]
    return [l for l in body if not l.startswith(('⎿', '✻', '● Compacted', '●  Compacted'))]


def message_tail(screen, n=4):
    """The last n non-empty lines above Claude's input box."""
    return _above_input(screen)[-n:]


def last_message(screen):
    """Lines of Claude's last message: from the last '●' bullet to the input box."""
    body = _above_input(screen)
    starts = [i for i, l in enumerate(body) if l.startswith('●')]
    return body[starts[-1]:] if starts else body[-12:]


ASK_RE = re.compile(r'\?\s*$|decision needed|^\W*you:|do you want|should i\b|would you like|'
                    r'waiting (?:on|for) (?:you|michal)|^not done\b|\bstill owed\b|\bblocked (?:on|by)\b', re.I)


def question_line(screen):
    """The line where Claude's last message asks for a decision or an action, else None."""
    for line in last_message(screen):
        if ASK_RE.search(line):
            return line.lstrip('●- ').strip()
    return None


def typed_prompt(screen):
    """Text currently typed in Claude's input box, or '' if the box is empty or missing."""
    lines = screen.rstrip('\n').split('\n')
    bars = _bars(lines)
    if len(bars) < 2:
        return ''
    block = [l for l in lines[bars[-2] + 1:bars[-1]]]
    if not block:
        return ''
    first = block[0].lstrip()
    if first.startswith('❯'):
        first = first[1:].lstrip()
        block = [first, *block[1:]]
    return '\n'.join(block).strip()


def classify_screen(screen, prompt_blank):
    """Class of a live Claude screen: working, menu, input, asking or idle.
    A blank prompt wins over 'input' only after the ghost suggestion is masked (input_view)."""
    tail = '\n'.join(screen.rstrip('\n').split('\n')[-40:])
    if BUSY_RE.search(tail):
        return 'working'
    if MENU_RE.search(tail):
        return 'menu'
    if question_line(screen):
        return 'asking'
    if not prompt_blank:
        return 'input'
    return 'idle'


def screen_key(screen):
    """Hash of a screen without its spinner/timer line and digits, to spot a stuck Claude."""
    lines = [l for l in screen.split('\n') if not BUSY_RE.search(l)]
    return hashlib.sha256(DIGITS_RE.sub('', '\n'.join(lines)).encode()).hexdigest()[:16]


def window_pr(window):
    """PR number from a window name such as code-382-avo-youtube-publish or ptw-352-..."""
    m = WINDOW_PR_RE.match(window or '')
    return int(m.group(1)) if m else None


def normalize_pane_path(path):
    """Strip tmux's ' (deleted)' suffix from a pane path."""
    return (path or '').removesuffix(' (deleted)')


def is_platform_worktree_path(path):
    """True if path is ~/ver/platform or a ~/ver/platform.<slug> worktree (possibly deleted)."""
    p = normalize_pane_path(path)
    root = str(PLATFORM_GIT)
    return p == root or p.startswith(root + '.') or p.startswith(root + '/')


def branch_slug(branch):
    return (branch or '').replace('/', '-')


def window_matches_pr(window, path, pr):
    """True if this agents window belongs to the PR: numbered name, legacy name, or worktree path."""
    number = pr['number']
    slug = branch_slug(pr.get('branch'))
    name = window or ''
    path = normalize_pane_path(path)
    if window_pr(name) == number:
        return True
    if slug and name in (slug, pr.get('branch'), 'claude-' + slug):
        return True
    if slug and path.rstrip('/').endswith('/platform.' + slug):
        return True
    return False


def windows_to_close(panes, prs):
    """Idle panes that belong to a MERGED or CLOSED PR and are safe to close.

    Never includes a pane whose command is claude or node, or whose worktree is
    dirty or ahead of its upstream. A missing worktree (already removed) is safe.
    """
    closed = {n: pr for n, pr in prs.items() if (pr.get('state') or '').upper() in ('MERGED', 'CLOSED')}
    out = []
    seen = set()
    for p in panes:
        cmd = p.get('command') or ''
        if p.get('claude') or cmd in ('claude', 'node'):
            continue
        wt = p.get('wt') or {}
        if wt.get('dirty') or (wt.get('ahead') or 0) > 0:
            continue
        for pr in closed.values():
            if window_matches_pr(p.get('window'), p.get('path'), pr):
                key = p.get('id') or p.get('window')
                if key not in seen:
                    seen.add(key)
                    out.append((p, pr))
                break
    return out


def milestone_rank(milestones):
    """0 if any of the milestone titles is a priority milestone, else 1."""
    return 0 if any((t or '').strip().lower() in PRIORITY_MILESTONES for t in milestones) else 1


def menu_line(screen):
    for line in reversed(screen.rstrip('\n').split('\n')[-30:]):
        s = line.strip()
        if s.endswith('?') or s.lower().startswith('do you want'):
            return s[:90]
    return 'numbered choice menu'


def codex_status(pr):
    """('clean' | 'findings' | 'pending', detail) for a PR from hr-gate's fetch()."""
    head = pr['headRefOid']
    def is_codex(node):
        return ((node.get('author') or {}).get('login') or '').startswith(CODEX)
    open_thr = [t for t in pr['reviewThreads']['nodes'] if not t['isResolved'] and not t['isOutdated']
                and t['comments']['nodes'] and is_codex(t['comments']['nodes'][0])]
    on_head = [r for r in pr['reviews']['nodes'] if is_codex(r) and r.get('commit')
               and r['commit']['oid'] == head and r['comments']['totalCount'] > 0]
    if open_thr or on_head:
        return 'findings', f"{len(open_thr)} open Codex thread(s)" + (f", {len(on_head)} finding review(s) on head" if on_head else '')
    for c in pr['comments']['nodes']:
        if not is_codex(c):
            continue
        body = c.get('body') or ''
        if 'Reviewed commit' in body and head[:8] in body and "Didn't find any major issues" in body:
            return 'clean', 'clean receipt on head'
        m = re.search(r'codex-review-state:v1\s*(\{.*?\})\s*-->', body, re.S)
        if m:
            try:
                st = json.loads(m.group(1)).get('code_review', {})
            except ValueError:
                continue
            if st.get('commit_sha') == head and not st.get('has_any_findings') and not st.get('has_unresolved_findings'):
                return 'clean', 'review-state clean on head'
    return 'pending', 'no Codex result on head'


def codex_requested(pr):
    """True if someone (not a bot) already posted '@codex review' after the head commit."""
    nodes = pr['commits']['nodes']
    if not nodes:
        return False
    head_at = nodes[0]['commit']['committedDate']
    for c in pr['comments']['nodes']:
        login = (c.get('author') or {}).get('login') or ''
        if (c.get('body') or '').strip().lower() == '@codex review' and not login.endswith('[bot]') \
                and c['createdAt'] > head_at:
            return True
    return False


ROW_REASONS = {
    '2 ': 'head conflicts with origin/main: needs a merge-main',
    '4 ': 'needs a MinBot pass on this head',
    '5 ': 'Linux CI not green',
    '6 ': 'staging redeploy needed (served commit lacks head, or body names none)',
    '7a': 'screenshots missing or older than the head/served commit',
    '7b': '"where to see it" line missing',
    '8 ': 'Michal commented after the head',
}
CI_WAIT_RE = re.compile(r'IN_PROGRESS|QUEUED|PENDING|WAITING|REQUESTED|no Linux GitHub checks', re.I)


def gate_reasons(rows):
    """Judgment reasons for hr-gate rows 1-8 that fail. A CI run still going is not one."""
    out = []
    for name, ok, why in rows:
        if ok or name.startswith(('1 ', '3 ', '9 ')):
            continue
        if name.startswith('5 ') and CI_WAIT_RE.search(why) and 'FAILURE' not in why.upper():
            continue
        key = next((k for k in ROW_REASONS if name.startswith(k)), None)
        out.append(f"{ROW_REASONS.get(key, name)} ({why[:110]})")
    return out


# ---------------------------------------------------------------------------
# shell, state and log

def sh(argv, check=False, timeout=300):
    r = subprocess.run([str(a) for a in argv], capture_output=True, text=True, timeout=timeout)
    if check and r.returncode:
        raise RuntimeError(f"{' '.join(str(a) for a in argv[:3])}...: {(r.stderr or r.stdout).strip()[:200]}")
    return r


def now_et():
    return datetime.now(ET).isoformat(timespec='seconds')


class State:
    """Small JSON file: once-keys for one-shot actions and per-pane screen hashes."""

    def __init__(self, path):
        self.path = path
        try:
            self.data = json.loads(path.read_text())
        except (OSError, ValueError):
            self.data = {}
        self.data.setdefault('done', {})
        self.data.setdefault('screens', {})

    def done(self, key):
        return key in self.data['done']

    def mark(self, key):
        self.data['done'][key] = now_et()
        self.save()

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix('.tmp')
        tmp.write_text(json.dumps(self.data, indent=1, sort_keys=True))
        os.replace(tmp, self.path)


def log(line):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(STATE_DIR / 'tick.log', 'a') as f:
        f.write(f"{now_et()}\t{line}\n")


def mn_relayed(n, head):
    """True if mn-workflow already sent this head's Codex findings to the Claude."""
    try:
        done = json.loads(MN_STATE.read_text()).get('done', {})
    except (OSError, ValueError):
        return False
    return f"codex_findings:tell:{REPO}#{n}@{head}" in done


def parked_windows():
    """swarm-park registry: {window: pr number as text ('' if none)}."""
    try:
        lines = PARK_REGISTRY.read_text().splitlines()
    except OSError:
        return {}
    rows = [l.split('\t') for l in lines if l and not l.startswith('#')]
    return {r[0]: (r[5] if len(r) > 5 else '') for r in rows}


# ---------------------------------------------------------------------------
# board

BOARD_Q = """query($c:String){organization(login:"tangentcode"){projectV2(number:4){items(first:100, after:$c){
 pageInfo{hasNextPage endCursor}
 nodes{id fieldValueByName(name:"Status"){...on ProjectV2ItemFieldSingleSelectValue{name optionId}}
  content{__typename
   ...on PullRequest{number state isDraft headRefName headRefOid title repository{nameWithOwner}
     body milestone{title} labels(first:20){nodes{name}}
     closingIssuesReferences(first:10){nodes{number milestone{title} labels(first:20){nodes{name}}}}}}}}}}}"""


def board_prs(g):
    out, cursor = {}, None
    active = board.active_fast_track()  # the ONE active fast-track ticket (kept by swarm-promoted)
    while True:
        data = g['gql'](BOARD_Q, c=cursor)['organization']['projectV2']['items']
        for it in data['nodes']:
            c = it.get('content') or {}
            if c.get('__typename') != 'PullRequest' or (c.get('repository') or {}).get('nameWithOwner') != REPO:
                continue
            col = board.column(it.get('fieldValueByName'))
            miles = [(c.get('milestone') or {}).get('title')]
            miles += [(i.get('milestone') or {}).get('title') for i in c['closingIssuesReferences']['nodes']]
            labels = {l['name'] for l in c['labels']['nodes']}
            issue_labels = {l['name'] for i in c['closingIssuesReferences']['nodes']
                            for l in ((i.get('labels') or {}).get('nodes') or [])}
            out[c['number']] = dict(number=c['number'], state=c['state'], draft=c['isDraft'],
                                    branch=c['headRefName'], head=c['headRefOid'], title=c['title'],
                                    body=c.get('body') or '',
                                    col=col, item=it['id'],
                                    rank=board.priority_rank(
                                        board.is_active_fast_track(
                                            c['number'], [i.get('number') for i in c['closingIssuesReferences']['nodes']],
                                            active),
                                                             milestone_rank(miles) == 0),
                                    parked=c['number'] in g['PARKED'] | EXTRA_PARKED or 'parked' in labels)
        if not data['pageInfo']['hasNextPage']:
            return out
        cursor = data['pageInfo']['endCursor']


# ---------------------------------------------------------------------------
# panes

PANE_FMT = '\t'.join(['#{pane_id}', '#{window_index}', '#{window_name}', '#{pane_current_command}',
                      '#{pane_current_path}', '#{pane_dead}'])


def git(path, *args):
    r = sh(['git', '-C', path, *args], timeout=60)
    return r.stdout.strip() if r.returncode == 0 else None


def worktree_facts(path):
    """Branch, HEAD, dirty and ahead-of-upstream for a platform worktree; None otherwise."""
    if not path or path.endswith(' (deleted)') or not os.path.isdir(path):
        return None
    common = git(path, 'rev-parse', '--path-format=absolute', '--git-common-dir')
    if not common or Path(common).resolve() != (PLATFORM_GIT / '.git').resolve():
        return None
    status = git(path, 'status', '--porcelain')
    ahead = git(path, 'rev-list', '--count', '@{u}..HEAD')
    return dict(top=git(path, 'rev-parse', '--show-toplevel'),
                branch=git(path, 'symbolic-ref', '--short', '-q', 'HEAD'),
                head=git(path, 'rev-parse', 'HEAD'),
                dirty=status is None or bool(status),
                ahead=int(ahead) if ahead and ahead.isdigit() else None)


def capture(pane_id):
    from . import agents
    r = sh(['tmux', 'capture-pane', '-p', '-e', '-t', pane_id], timeout=30)
    raw = r.stdout or ''
    return agents.strip_ansi(raw), agents.claude_prompt_blank(agents.input_view(raw))


def is_claude(pane_id, command):
    if command == 'claude':
        return True
    if command in SHELLS:
        return False
    from . import agents
    try:
        return agents.pane_agent(pane_id).get('agent') == 'claude'
    except Exception:
        return False


def read_panes(state):
    r = sh(['tmux', 'list-panes', '-s', '-t', SESSION, '-F', PANE_FMT], timeout=30)
    if r.returncode:
        raise RuntimeError(f"cannot list tmux session {SESSION!r}: {r.stderr.strip()}")
    parked = parked_windows()
    panes, seen = [], set()
    now = time.time()
    for line in r.stdout.splitlines():
        pid, widx, window, cmd, path, dead = line.split('\t')
        if dead not in ('', '0'):
            continue
        wt = worktree_facts(path)
        if wt is None and not is_platform_worktree_path(path):
            continue
        p = dict(id=pid, window=window, command=cmd, path=path, wt=wt, parked=window in parked,
                 claude=is_claude(pid, cmd), cls=None, screen='')
        if p['claude']:
            screen, blank = capture(pid)
            cls = classify_screen(screen, blank)
            if cls in ('idle', 'asking'):
                time.sleep(SETTLE)
                screen2, blank2 = capture(pid)
                if screen2 != screen or classify_screen(screen2, blank2) != cls:
                    cls = 'working'
            p['screen'] = screen
            if cls == 'working':
                key = screen_key(screen)
                prev = state.data['screens'].get(pid)
                if prev and prev.get('key') == key:
                    p['stuck_min'] = int((now - prev['since']) / 60)
                else:
                    state.data['screens'][pid] = {'key': key, 'since': now}
                seen.add(pid)
            elif cls == 'idle':
                if wt is None:
                    cls = 'finished'
                else:
                    cls = 'finished' if not wt['dirty'] and wt['ahead'] == 0 else 'idle-dirty'
            p['cls'] = cls
        else:
            p['cls'] = 'shell'
        panes.append(p)
    state.data['screens'] = {k: v for k, v in state.data['screens'].items() if k in seen}
    return panes


def attach_prs(panes, prs):
    by_branch = {p['branch']: n for n, p in prs.items() if p['state'] == 'OPEN'}
    for p in panes:
        n = by_branch.get((p['wt'] or {}).get('branch'))
        if n is None:
            n = window_pr(p['window'])
            if n not in prs:
                n = None
        p['pr'] = n


# ---------------------------------------------------------------------------
# the pass

class Tick:
    def __init__(self, dry_run=False, verbose=False, out=print):
        self.dry = dry_run
        self.verbose = verbose
        self.out = out
        self.did = []
        self.notes = []
        os.environ['PATH'] = f"{BIN}{os.pathsep}{os.environ.get('PATH', '')}"
        self.g = runpy.run_path(str(BIN / 'hr-gate'), run_name='swarm_tick')
        self.state = State(STATE_DIR / 'state.json')

    def note(self, n, pane, reason):
        where = f"{pane['id']} {pane['window']}" if pane else '-'
        self.notes.append((n, f"#{n} {where}: {reason}" if n else f"{where}: {reason}"))

    def change(self, text):
        self.did.append(('would: ' if self.dry else 'did: ') + text)
        if not self.dry:
            log('did\t' + text)

    def tool(self, argv):
        r = sh(argv, timeout=1800)
        out = (r.stdout or '') + (r.stderr or '')
        log(f"tool\t{' '.join(str(a) for a in argv)}\trc={r.returncode}\n" + out.rstrip())
        if self.verbose:
            self.out(f"--- {' '.join(str(a) for a in argv)} (rc={r.returncode})\n{out.rstrip()}")
        return r.returncode, out

    # step 1 ---------------------------------------------------------------
    def read(self):
        self.prs = board_prs(self.g)
        self.panes = read_panes(self.state)
        attach_prs(self.panes, self.prs)
        if self.verbose:
            for p in self.panes:
                col = self.prs[p['pr']]['col'] if p['pr'] else '-'
                wt = p['wt'] or {}
                self.out(f"pane {p['id']} {p['window']}: {p['cls']} pr={p['pr']} card={col} branch={wt.get('branch')} "
                         f"dirty={wt.get('dirty')} ahead={wt.get('ahead')}")

    # step 1b: every working PR carries its brief ---------------------------
    def brief_pass(self):
        working = {n: pr for n, pr in self.prs.items()
                   if pr['state'] == 'OPEN' and not pr['parked'] and pr['col'] in WORKING_COLS}
        for n, has_file in brief.missing_briefs(working):
            if not has_file:
                self.note(n, None, f"{working[n]['col']} with no brief in the PR body and no brief file "
                                   f"{brief.brief_path(n)}: write the brief")
                continue
            if self.dry:
                self.change(f"#{n} write the brief into the PR body")
                continue
            try:
                what = brief.write_brief(n, REPO)
            except (RuntimeError, subprocess.TimeoutExpired) as e:
                self.note(n, None, f"failed to write the brief into the PR body: {e}")
                continue
            if what == 'wrote':
                self.change(f"#{n} wrote the brief into the PR body")

    # step 2 + pane judgment -------------------------------------------------
    def panes_pass(self, scratch):
        for n, pr in sorted(self.prs.items(), key=lambda kv: (kv[1]['rank'], kv[0])):
            if pr['state'] != 'OPEN' or pr['parked'] or pr['col'] not in WORKING_COLS:
                continue
            mine = [p for p in self.panes if p['pr'] == n]
            claudes = [p for p in mine if p['claude']]
            if len(claudes) > 1:
                self.note(n, claudes[0], 'duplicate Claudes on one PR: ' + ', '.join(p['id'] for p in claudes))
            if not claudes:
                self.no_claude(n, pr, mine)
                continue
            c = claudes[0]
            if c['cls'] == 'working':
                if c.get('stuck_min', 0) >= STUCK_MINUTES:
                    self.note(n, c, f"working but the screen has not changed for {c['stuck_min']} min (stuck?)")
            elif c['cls'] == 'menu':
                self.note(n, c, f"at a menu: {menu_line(c['screen'])}")
            elif c['cls'] == 'input':
                typed = typed_prompt(c.get('screen') or '')
                preview = (typed.replace('\n', ' ')[:110] or '(empty capture)')
                if self.dry:
                    self.change(f"#{n} submit typed prompt in {c['id']}: {preview}")
                else:
                    r = sh(['tmux', 'send-keys', '-t', c['id'], 'Enter'])
                    if r.returncode:
                        self.note(n, c, 'failed to submit typed prompt: '
                                        + ((r.stderr or r.stdout or '').strip()[:120] or f'rc={r.returncode}'))
                    else:
                        self.change(f"#{n} submitted typed prompt in {c['id']}: {preview}")
            elif c['cls'] == 'asking':
                self.note(n, c, 'idle, asks for a decision or action: ' + (question_line(c['screen']) or '?')[:110])
            elif c['cls'] == 'idle-dirty':
                bits = (['uncommitted changes'] if c['wt']['dirty'] else []) + \
                       ([f"{c['wt']['ahead']} unpushed commit(s)"] if c['wt']['ahead'] else []) + \
                       (['no upstream'] if c['wt']['ahead'] is None else [])
                self.note(n, c, 'idle with ' + ', '.join(bits))
            elif c['cls'] == 'finished':
                self.finished(n, pr, c, scratch)

    def no_claude(self, n, pr, mine):
        if pr['col'] != 'In Progress':
            return
        shells = [p for p in mine if p['cls'] == 'shell']
        if not shells:
            parked = [w for w, num in parked_windows().items() if num == str(n)]
            if parked:
                self.note(n, None, f"parked by swarm-park (window {parked[0]} is gone) while In Progress: "
                                   f"resume it with swarm-resume, or mark it ready if the work is done")
            else:
                self.note(n, None, f"In Progress with no pane in tmux session {SESSION} (no Claude working on it)")
            return
        s = shells[0]
        wt = s['wt']
        if wt['dirty'] or wt['ahead']:
            self.note(n, s, 'Claude exited mid-work: worktree has uncommitted or unpushed changes')
        elif wt['head'] != pr['head']:
            self.note(n, s, f"Claude exited; local HEAD {(wt['head'] or '?')[:8]} is not PR head {pr['head'][:8]}")
        elif not s['parked'] and self.never_started(n, pr, s):
            return
        else:
            how = 'parked by swarm-park' if s['parked'] else 'exited'
            self.note(n, s, f"Claude {how}, worktree clean and pushed, PR still {'draft ' if pr['draft'] else ''}In Progress: resume with the open work, or mark ready")

    def never_started(self, n, pr, s):
        """A held launch left behind: an idle shell whose head is still the scaffold commit.

        swarm-start-scoped leaves the shell window when memory holds the launch. If the
        head is still the "Start #N" commit and a brief file exists, queue the launch."""
        subject = git(s['wt'].get('top') or s.get('path'), 'log', '-1', '--format=%s') or ''
        path = brief.brief_path(n)
        if not SCAFFOLD_RE.match(subject) or brief.read_brief(n) is None:
            return False
        queued = any(r['branch'] == pr['branch'] for r in launches.read_queue())
        if not queued:
            if not self.dry:
                launches.hold(pr['branch'], s['window'], n, str(path))
            self.change(f"#{n} queued held launch {pr['branch']} (shell {s['id']}, head is the scaffold commit)")
        return True

    def finished(self, n, pr, c, scratch):
        if c['wt']['head'] != pr['head']:
            self.note(n, c, f"idle; local HEAD {(c['wt']['head'] or '?')[:8]} is not PR head {pr['head'][:8]}")
            return
        full = self.g['fetch'](n)
        status, detail = codex_status(full)
        if status == 'findings':
            if mn_relayed(n, pr['head']):
                self.note(n, c, f"finished without a new push after mn-workflow relayed Codex findings ({detail})")
            else:
                self.note(n, c, f"Codex findings to relay ({detail})")
            return
        if pr['col'] != 'In Progress':
            return  # Auto Review: waiting on Codex; a clean result is handled by the hr-gate step
        try:
            scratch.ensure_main()
            scratch.ensure(pr['head'])
            clean = self.g['merge_tree_clean'](scratch.dir, scratch.main, pr['head'])
        except RuntimeError as e:
            self.note(n, c, f"finished, but the merge check failed: {e}")
            return
        if not clean:
            self.note(n, c, 'finished, but the head conflicts with origin/main (relay a merge-main)')
            return
        if status == 'clean' and not pr['draft']:
            return  # the hr-gate step handles a ready, Codex-clean PR
        self.ready_for_review(n, pr, full, codex_clean=status == 'clean')

    def ready_for_review(self, n, pr, full, codex_clean=False):
        head = pr['head']
        if not self.dry:
            live = self.g['fetch'](n, refresh=True)
            _, col = self.g['card'](live)
            if live['headRefOid'] != head or col != 'In Progress':
                log(f"skip\t#{n} changed during the pass (head {live['headRefOid'][:8]}, card {col})")
                return
            full = live
        if pr['draft']:
            if not self.dry:
                sh(['gh', 'pr', 'ready', str(n), '-R', REPO], check=True)
            self.change(f"#{n} marked ready (Claude finished, head {head[:8]} pushed)")
        if not self.dry:
            self.g['move'](pr['item'], 'Auto Review')
        self.change(f"#{n} card In Progress -> Auto Review")
        key = f"codex-request:{n}@{head}"
        if codex_clean or self.state.done(key) or codex_requested(full):
            return
        if not self.dry:
            sh(['gh', 'pr', 'comment', str(n), '-R', REPO, '--body', '@codex review'], check=True)
            self.state.mark(key)
        self.change(f"#{n} posted \"@codex review\" for head {head[:8]}")

    # step 3 ---------------------------------------------------------------
    def hr_pass(self, scratch):
        cands = []
        for n, pr in self.prs.items():
            if pr['state'] != 'OPEN' or pr['parked'] or pr['draft'] or pr['col'] not in WORKING_COLS:
                continue
            full = self.g['fetch'](n)
            if codex_status(full)[0] != 'clean':
                continue
            entered = self.g['entered_col'](full, (pr['col'],))
            cands.append((pr['rank'], entered or datetime(1970, 1, 1, tzinfo=timezone.utc), n, full))
        cands.sort(key=lambda t: t[:3])
        hr_peers = self.g['list_hr_prs'](refresh=True)
        for _, _, n, full in cands:
            mine = sorted((p for p in self.panes if p['pr'] == n), key=lambda p: not p['claude'])
            pane = mine[0] if mine else None
            try:
                rows, _ = self.g['evaluate'](full, hr_peers=hr_peers, scratch=scratch)
            except RuntimeError as e:
                self.note(n, pane, f"hr-gate check failed: {e}")
                continue
            if not self.g['normal_gates_pass'](rows):
                for reason in gate_reasons(rows):
                    self.note(n, pane, 'Codex clean; ' + reason)
                continue
            if self.dry:
                self.change(f"#{n} hr-gate --apply {n} (conditions 1-8 pass)")
                continue
            rc, out = self.tool([BIN / 'hr-gate', '--apply', str(n)])
            if rc == gatelock.EXIT_BUSY:
                self.note(None, None, f"hr-gate --apply {n} skipped, gate lock {out.strip()[:150]}; next pass retries")
                break
            msg = [l.strip() for l in out.splitlines() if re.match(r'\s+(moved|refused|stayed|stays|#\d+ has no)', l)]
            self.change(f"#{n} hr-gate --apply: " + (msg[-1] if msg else f"rc={rc}"))
            if any('stayed' in m and 'rebased onto' in m for m in msg):
                self.note(n, pane, f"needs a speculative vN branch: relay /tmp/relay-{n}-spec.md to its Claude")
            hr_peers = self.g['list_hr_prs'](refresh=True)

    # step 3b: held launches and memory pressure ---------------------------
    def launch_pass(self, now=None):
        now = time.time() if now is None else now
        try:
            m = launches.memory()
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as e:
            self.note(None, None, f"cannot read worker memory: {e}")
            return
        by_branch = {p['branch']: n for n, p in self.prs.items() if p['state'] == 'OPEN'}
        rows = launches.read_queue()
        for r in rows:
            r['pr'] = r.get('pr') or by_branch.get(r['branch'])
        rank = lambda pr: self.prs.get(pr, {}).get('rank', 1)  # noqa: E731
        rows = launches.ordered(rows, rank)
        ok = launches.admits(m)
        since = self.state.data.get('pressure_since')
        if ok:
            self.state.data.pop('pressure_since', None)
        else:
            since = since or now
            self.state.data['pressure_since'] = since
            mins = int((now - since) // 60)
            if mins >= PRESSURE_MINUTES:
                held = ', '.join(f"#{r['pr']}" if r['pr'] else r['branch'] for r in rows) or 'none'
                self.note(None, None, f"memory pressure for {mins} min: {launches.describe(m)}; "
                                      f"held launches: {held}")
        if not rows or not ok:
            return
        first = rows[0]
        name = f"#{first['pr']}" if first['pr'] else first['branch']
        if self.dry:
            self.change(f"{name} start held launch {first['branch']} ({launches.describe(m)})")
            return
        what, row, out = launches.start_next(rank=rank, mem=lambda: m)
        if what == 'launched':
            self.change(f"{name} started held launch in {out.get('window')} ({out.get('pane')})")
            if out.get('brief_error'):
                self.note(first['pr'], None, f"started, but the brief line was not sent: {out['brief_error']}")
        elif what == 'refused':
            self.note(first['pr'], None, f"held launch {first['branch']} refused and dropped from the queue: "
                                         f"{out.get('refused', out)}")

    # step 4 ---------------------------------------------------------------
    def active_pass(self):
        if self.dry:
            a = runpy.run_path(str(BIN / 'active-gate'), run_name='swarm_tick')
            for it in a['project_items']():
                typ, num, title, state, _nid, item_id, col = a['classify_item'](it)
                if col != 'Active':
                    continue
                ev = a['evaluate_item'](it)
                if ev['ok'] or ev['parked'] or ev['kind'] == 'unassigned':
                    continue
                dest = a['demote_status'](ev['kind'])
                if dest and dest != col:
                    self.change(f"#{num} active-gate: Active -> {dest} ({ev['kind']})")
            return
        for argv in ([BIN / 'active-gate', '--sweep', '--demote'], [BIN / 'active-gate', '--promote-missing']):
            rc, out = self.tool(argv)
            cur = None
            for line in out.splitlines():
                m = re.match(r'^(?:PARKED )?#(\d+) ', line)
                if m:
                    cur = m.group(1)
                if '-> moved to' in line and cur:
                    self.change(f"#{cur} active-gate: Active {line.strip()[3:]}")
                elif '-> Active' in line:
                    self.change('active-gate: ' + ' '.join(line.split()))
            if rc == 2:
                self.note(None, None, f"{argv[0].name} error: {out.strip().splitlines()[-1][:150] if out.strip() else rc}")

    # step 5: the just-in-time flow (methodology#56) --------------------------
    def flow_pass(self):
        """Promote into Human Review and pull one Ready PR into Staging (swarm-flow step)."""
        f = flow.Flow(dry_run=self.dry, out=lambda line: None, g=self.g)
        try:
            if self.dry:
                f.step()
            else:
                with gatelock.gate_lock('swarm-tick flow step'):
                    f.step()
        except gatelock.Busy as e:
            self.note(None, None, f"flow step skipped, gate lock busy: {e}; next pass retries")
            return
        except (RuntimeError, subprocess.TimeoutExpired, OSError) as e:
            self.note(None, None, f"flow step error: {e}")
            return
        for line in f.did:
            self.did.append(line)
        for line in f.notes:
            self.note(None, None, 'flow: ' + line)

    # retired by methodology#56: kept for reference, no longer called ------------
    def pending_pass(self):
        argv = [BIN / 'pending-promote', '--dry-run' if self.dry else '--apply']
        rc, out = self.tool(argv)
        if rc == gatelock.EXIT_BUSY:
            self.note(None, None, f"pending-promote skipped, gate lock {out.strip()[:150]}; next pass retries")
            return
        pat = (r'would run: hr-gate|would pass hr-gate|would enter STACKED|FORCE-PUSH STEPS'
               if self.dry else r'moved #|force-updated|saved \w|reset ')
        for line in out.splitlines():
            s = line.strip()
            if re.search(pat, s):
                self.change('pending-promote: ' + s)
            elif 'refill needed' in s and not any('refill needed' in t for _, t in self.notes):
                m = re.search(r'Pending \d+/\d+', s)
                self.note(None, None, (m.group(0) if m else 'Pending') + ': refill needed (launch approved briefs)')
            elif re.search(r'relay|not on origin yet|no vN tip', s, re.I) and '#' in s:
                self.note(None, None, 'pending-promote: ' + s[:150])
        if rc == 2:
            self.note(None, None, f"pending-promote error: {out.strip().splitlines()[-1][:150] if out.strip() else rc}")

    # step 6: finished Claudes whose PR has moved on -------------------------
    def closable(self):
        for p in self.panes:
            if not p['claude'] or p['cls'] != 'finished':
                continue
            pr = self.prs.get(p['pr']) if p['pr'] else None
            wt = p['wt'] or {}
            if pr is None:
                self.note(None, p, f"finished; no open board PR for branch {wt.get('branch')}: can be closed")
            elif pr['state'] != 'OPEN' or pr['col'] in LATER_COLS:
                where = pr['col'] if pr['state'] == 'OPEN' else pr['state'].lower()
                self.note(pr['number'], p, f"finished; PR is {where}: Claude can be closed")
        for p, pr in windows_to_close(self.panes, self.prs):
            label = f"window {p['window']} ({p['id']}) for {pr['state'].lower()} PR #{pr['number']}"
            if self.dry:
                self.change(f"close {label}")
                continue
            r = sh(['tmux', 'kill-window', '-t', p['id']])
            if r.returncode:
                self.note(pr['number'], p, f"failed to close window: {(r.stderr or r.stdout).strip()[:120]}")
            else:
                self.change(f"closed {label}")

    def run(self):
        self.read()
        self.brief_pass()
        with self.g['ScratchRepo']() as scratch:
            self.panes_pass(scratch)
        self.launch_pass()
        self.active_pass()
        self.flow_pass()
        self.closable()
        if not self.dry:
            self.state.save()
        for line in self.did:
            self.out(line)
        seen = set()
        for _, line in sorted(self.notes, key=lambda t: (self.prs.get(t[0], {}).get('rank', 1), t[0] or 0)):
            if line not in seen:
                seen.add(line)
                self.out(line)
        return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog='swarm-tick', description=__doc__.split('\n', 1)[0])
    ap.add_argument('--dry-run', action='store_true', help='change nothing; print what would change')
    ap.add_argument('--verbose', action='store_true', help='print pane classes and tool output')
    a = ap.parse_args(argv)
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    lock = open(LOCK, 'a+')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print('swarm-tick: another pass is running', file=sys.stderr)
        return 0
    try:
        return Tick(dry_run=a.dry_run, verbose=a.verbose).run()
    except (RuntimeError, subprocess.TimeoutExpired) as e:
        print(f"swarm-tick error: {e}", file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
