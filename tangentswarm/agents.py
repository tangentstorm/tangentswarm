"""Talking to coding agents (Claude Code, Codex, Gemini, ...) that run in tmux.

Port of scialect's src/agents/{tui-agent,claude-cli,codex-cli,gemini-cli}.mts
plus the agent detection from tell-worker.mts / local-status.mts and the
send sequence used by every tell-worker handoff:

    reach an empty prompt (space probe) -> [/new, 500ms, Enter, 10s]
    -> type the text literally -> 500ms -> Enter

The prompt-detection functions are pure (screen text in, bool out) so they
can be unit-tested without tmux.
"""
import hashlib
import os
import re
import subprocess
import time

from . import registry, tmux
from .workers import load_known_agents

PROBE_SETTLE = 0.8       # after the space probe
PROBE_RECHECK = 0.3
PROBE_CLEANUP = 0.1      # after BSpace
ENTER_DELAY = 0.5        # between text and Enter (TUIs drop fast Enters)
NEW_CONVERSATION_WAIT = 10.0   # after "/new" + Enter


# ---------------------------------------------------------------------------
# prompt-blank detection (pure functions over capture-pane text)

def _bar_lines(lines):
    """Indices of lines that are mostly a long horizontal bar (─)."""
    return [i for i, line in enumerate(lines)
            if '─' in line and len(line.replace('─', '').strip()) < 15]


def claude_prompt_blank(screen):
    """Claude Code: the input box sits between the two lowest ─ bars and
    starts with ❯. Blank when nothing follows the ❯."""
    lines = screen.split('\n')
    bars = _bar_lines(lines)
    if len(bars) < 2:
        for line in reversed(lines):
            pos = line.find('❯')
            if pos != -1:
                return line[pos + 1:].strip() == ''
        return False
    for line in lines[bars[-2] + 1:bars[-1]]:
        pos = line.find('❯')
        if pos != -1:
            return line[pos + 1:].strip() == ''
    return False


def codex_prompt_blank(screen):
    """Codex: the last line containing › is the live input prompt."""
    for line in reversed(screen.split('\n')):
        if '›' in line:
            return line[line.find('›') + 1:].strip() == ''
    return False


def gemini_prompt_blank(screen):
    """Gemini: like Claude, but the prompt symbol is '>' and the lowest '>'
    between the two bottom bars wins."""
    lines = screen.split('\n')
    bars = _bar_lines(lines)
    if len(bars) < 2:
        for line in reversed(lines):
            pos = line.find('>')
            if pos != -1:
                return line[pos + 1:].strip() == ''
        return False
    prompt_line = ''
    for line in lines[bars[-2] + 1:bars[-1]]:
        if '>' in line:
            prompt_line = line
    if prompt_line:
        return prompt_line[prompt_line.find('>') + 1:].strip() == ''
    return False


def muse_prompt_blank(screen):
    """Muse Code: same layout as Claude Code -- a ❯ input line between the two lowest
    ─ bars, then a footer ('<model> · <effort> · <path> · Auto-review'). Muse draws a
    placeholder ('Ask to monitor ...') in grey after the ❯, so feed this the
    input_view() of an escape-coded capture; on plain text the placeholder reads as
    typed input (the space probe then sees past it)."""
    return claude_prompt_blank(screen)


def is_muse_screen(screen):
    """Muse's footer line: '<model> · <effort> · <path> · <approval mode>'."""
    lines = [l for l in screen.split('\n') if l.strip()]
    return any(re.match(r'^\s*muse-[\w.-]+ · ', l) for l in lines[-4:])


PROMPT_DETECTORS = {
    'claude': claude_prompt_blank,
    'codex': codex_prompt_blank,
    'gemini': gemini_prompt_blank,
    'muse': muse_prompt_blank,
}


def detector_for(agent):
    """Map a detected agent name to its prompt detector (codex matches by
    substring, as in tell-worker.mts)."""
    a = (agent or '').lower()
    if 'codex' in a:
        return codex_prompt_blank
    return PROMPT_DETECTORS.get(a)


# ---------------------------------------------------------------------------
# escape-coded captures: tell placeholder/suggestion text from real input

_ESC_RE = re.compile(r'\x1b(?:\[([0-9;:?]*)([@-~])|\][^\x07\x1b]*(?:\x07|\x1b\\)|[()][0-9A-Za-z]|.)', re.S)
# Grey foregrounds TUIs use for placeholders and suggestions (plus SGR 2, dim).
_GREY_256 = set(range(239, 247)) | {8}
_KEEP = set('❯›>') | {chr(c) for c in range(0x2500, 0x2580)}   # prompt glyphs + box drawing


def strip_ansi(text):
    return _ESC_RE.sub('', text)


def input_view(text):
    """Plain text of an escape-coded capture (capture-pane -e) with every character
    drawn dim or in grey blanked out, except prompt glyphs and box drawing. Claude Code
    (prompt suggestions), Muse (placeholder) and Codex draw not-yet-typed hints that way,
    while typed input is drawn in the normal colour."""
    out = []
    dim = False
    fg = None
    pos = 0
    for m in _ESC_RE.finditer(text):
        chunk = text[pos:m.start()]
        pos = m.end()
        if chunk:
            out.append(_mask(chunk, dim, fg))
        if m.group(2) != 'm':
            continue
        params = [p for p in re.split('[;:]', m.group(1) or '0')]
        i = 0
        while i < len(params):
            p = params[i] or '0'
            if p == '0':
                dim, fg = False, None
            elif p == '2':
                dim = True
            elif p == '22':
                dim = False
            elif p == '39':
                fg = None
            elif p in ('38', '48') and i + 1 < len(params):
                if params[i + 1] == '5' and i + 2 < len(params):
                    if p == '38':
                        fg = ('256', params[i + 2])
                    i += 2
                elif params[i + 1] == '2':
                    if p == '38':
                        fg = ('rgb', tuple(params[i + 2:i + 5]))
                    i += 4
            elif p.isdigit() and (30 <= int(p) <= 37 or 90 <= int(p) <= 97):
                fg = ('16', p)
            i += 1
    if pos < len(text):
        out.append(_mask(text[pos:], dim, fg))
    return ''.join(out)


def _is_hint(dim, fg):
    if dim:
        return True
    if fg is None:
        return False
    kind, v = fg
    if kind == '256':
        return v.isdigit() and int(v) in _GREY_256
    if kind == '16':
        return v == '90'
    return False


def _mask(chunk, dim, fg):
    if not _is_hint(dim, fg):
        return chunk
    return ''.join(c if (c in _KEEP or c == '\n') else ' ' for c in chunk)


def screen_hash(screen):
    return hashlib.sha256(screen.encode('utf-8', errors='replace')).hexdigest()


# ---------------------------------------------------------------------------
# TUI agent wrapper (TuiAgent / ClaudeTui / CodexTui / GeminiTui)

class TuiAgent:
    """A TUI agent in a tmux pane. Subclasses set `detect`."""

    name = 'tui'
    detect = staticmethod(lambda screen: False)

    def __init__(self, target, capture=None, send_literal=None, send_key=None, sleep=time.sleep):
        self.target = target
        # escape-coded capture -> input_view(), so placeholders read as blank
        self._capture = capture or (lambda t: input_view(tmux.capture_pane(t, escapes=True)))
        self._send_literal = send_literal or (lambda t, s: tmux.send_keys_literal(t, s, enter=False))
        self._send_key = send_key or (lambda t, k: tmux.send_keys(t, k, enter=False))
        self._sleep = sleep

    def screen(self):
        return self._capture(self.target)

    def is_prompt_blank(self):
        return self.detect(self.screen())

    def ensure_prompt_is_empty(self):
        """Non-destructive space probe (tui-agent.mts#ensurePromptIsEmpty).

        Blank -> True.  Otherwise type a space: if the prompt now reads as
        blank, the old text was placeholder/suggestion filler -> True; else
        there is real user input -> False.  The space is always backspaced.
        """
        if self.is_prompt_blank():
            return True
        self._send_literal(self.target, ' ')
        self._sleep(PROBE_SETTLE)
        now_blank = self.is_prompt_blank()
        if not now_blank:
            self._sleep(PROBE_RECHECK)
            now_blank = self.is_prompt_blank()
        self._send_key(self.target, 'BSpace')
        self._sleep(PROBE_CLEANUP)
        return now_blank

    def send_text(self, text, enter=True):
        """Type text literally, pause ENTER_DELAY, then press Enter separately."""
        self._send_literal(self.target, text)
        if enter:
            self._sleep(ENTER_DELAY)
            self._send_key(self.target, 'Enter')

    def new_conversation(self):
        """'/new', 500ms, Enter, then wait 10s for the fresh session."""
        self.send_text('/new')
        self._sleep(NEW_CONVERSATION_WAIT)


class ClaudeTui(TuiAgent):
    name = 'claude'
    detect = staticmethod(claude_prompt_blank)


class CodexTui(TuiAgent):
    name = 'codex'
    detect = staticmethod(codex_prompt_blank)


class GeminiTui(TuiAgent):
    name = 'gemini'
    detect = staticmethod(gemini_prompt_blank)


class MuseTui(TuiAgent):
    """Muse Code: Enter submits; /new (or /clear) starts a fresh session."""
    name = 'muse'
    detect = staticmethod(muse_prompt_blank)


def tui_for(agent, target, **kw):
    """TUI wrapper for a detected agent name, or None if unsupported."""
    a = (agent or '').lower()
    if 'codex' in a:
        return CodexTui(target, **kw)
    if a == 'claude':
        return ClaudeTui(target, **kw)
    if a == 'gemini':
        return GeminiTui(target, **kw)
    if a == 'muse':
        return MuseTui(target, **kw)
    return None


# ---------------------------------------------------------------------------
# agent detection

def match_agent(rules, command, args, title):
    """local-status.mts#matchAgent: first rule whose constraints all hold."""
    cmd_base = command.split('/')[-1] if command else command
    for rule in rules:
        m = rule.get('match', {})
        if m.get('command') and cmd_base != m['command']:
            continue
        if m.get('args_contains') and m['args_contains'] not in args:
            continue
        if m.get('title_contains') and m['title_contains'] not in title:
            continue
        return rule.get('name')
    return None


def _run(argv):
    r = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                       stderr=subprocess.DEVNULL, text=True, check=False)
    return r.stdout if r.returncode == 0 else ''


def _pane_rows(window_target, fmt):
    sep = '\t'
    out = _run(['tmux', 'list-panes', '-t', window_target, '-F', sep.join(fmt)])
    return [line.split(sep, len(fmt) - 1) for line in out.splitlines() if line.strip()]


def process_info(pid):
    line = _run(['ps', '-p', str(pid), '-o', 'comm=,args=']).strip()
    if ' ' not in line:
        return line, ''
    command, args = line.split(' ', 1)
    return command, args.strip()


def child_pids(pid):
    return [p for p in _run(['pgrep', '-P', str(pid)]).split() if p]


def detect_agent(target, rules=None, cdir=None):
    """tell-worker.mts#detectAgent: look at every process on the first pane's
    tty and apply the known-agents rules.  target may be 'sess:win' or a pane."""
    rules = rules if rules is not None else load_known_agents(cdir)
    # list-panes on a window or pane target lists that window's panes; the
    # first row is the main pane, which is what scialect inspects.
    rows = _pane_rows(target, ['#{pane_tty}', '#{pane_current_command}', '#{pane_title}'])
    if not rows or len(rows[0]) < 3:
        return None
    tty, command, title = rows[0]
    if not tty or not command:
        return None
    all_procs = _run(['ps', '-t', tty, '-o', 'comm=,args='])
    cmd_base = command.split('/')[-1]
    for rule in rules:
        m = rule.get('match', {})
        if m.get('command') and cmd_base != m['command'] and m['command'] not in all_procs:
            continue
        if m.get('args_contains') and m['args_contains'] not in all_procs:
            continue
        if m.get('title_contains') and m['title_contains'] not in title:
            continue
        return rule.get('name')
    return None


def find_agent_in_window(session, window, rules=None, cdir=None):
    """local-status.mts#findAgentInWindow: check each pane's own process and
    its children. Returns (agent or None, live cwd or None)."""
    rules = rules if rules is not None else load_known_agents(cdir)
    rows = _pane_rows(f"{session}:{window}",
                      ['#{pane_index}', '#{pane_pid}', '#{pane_current_command}',
                       '#{pane_title}', '#{pane_current_path}'])
    for row in rows:
        if len(row) < 5:
            continue
        _, pid, _, title, path = row
        command, args = process_info(pid)
        agent = match_agent(rules, command, args, title)
        if agent:
            return agent, path
        for child in child_pids(pid):
            command, args = process_info(child)
            agent = match_agent(rules, command, args, title)
            if agent:
                return agent, path
    if rows and len(rows[0]) >= 5:
        return None, rows[0][4]
    return None, None


# ---------------------------------------------------------------------------
# pane-exact detection of registered agents (the typing guard)

class AgentNotReady(RuntimeError):
    pass


class NotAnAgentPane(AgentNotReady):
    """The pane's foreground process is not a registered coding agent."""


def foreground_processes(tty):
    """[(pid, stat, comm, args)] for every process on a tty (comm from /proc, untruncated
    by spaces in args)."""
    rows = []
    for line in _run(['ps', '-t', tty, '-o', 'pid=,stat=,args=']).splitlines():
        parts = line.split(None, 2)
        if len(parts) < 2:
            continue
        pid, stat = parts[0], parts[1]
        args = parts[2] if len(parts) > 2 else ''
        try:
            with open(f'/proc/{pid}/comm', encoding='utf-8', errors='replace') as f:
                comm = f.read().strip()
        except OSError:
            comm = os.path.basename(args.split(None, 1)[0]) if args else ''
        rows.append((pid, stat, comm, args))
    return rows


def resolve_pane(target):
    """The exact pane a tmux target means (what send-keys would hit):
    {'pane_id', 'tty', 'dead', 'session', 'window_index', 'pane_index'}."""
    sep = '\t'
    fmt = sep.join(['#{pane_id}', '#{pane_tty}', '#{pane_dead}', '#{session_name}',
                    '#{window_index}', '#{pane_index}'])
    parts = tmux.display(target, fmt).split(sep)
    if len(parts) < 6 or not parts[0].startswith('%'):
        raise NotAnAgentPane(f"{target}: no such pane")
    return {'pane_id': parts[0], 'tty': parts[1], 'dead': parts[2] not in ('', '0'),
            'session': parts[3], 'window_index': parts[4], 'pane_index': parts[5]}


def pane_agent(target, procs=None):
    """Registered agent in the foreground of the exact pane `target` resolves to.
    Returns the resolve_pane() dict plus 'agent' (None if none) and 'agent_pid'."""
    try:
        info = resolve_pane(target)
    except tmux.TmuxError as e:
        raise NotAnAgentPane(f"{target}: {e.stderr or 'no such pane'}") from e
    agent, pid = None, None
    if not info['dead'] and info['tty']:
        agent, pid = registry.agent_from_processes(
            procs if procs is not None else foreground_processes(info['tty']))
    return {**info, 'agent': agent, 'agent_pid': pid}


def require_agent_pane(target, procs=None):
    """pane_agent(), but raise NotAnAgentPane unless a registered agent is in the
    foreground. Typing tools send to the returned pane_id, not the raw target."""
    info = pane_agent(target, procs=procs)
    if not info['agent']:
        raise NotAnAgentPane(
            f"{target} ({info['pane_id']}) is not running a registered coding agent "
            f"({', '.join(registry.AGENT_NAMES)}) in the foreground; refusing to type into it")
    return info


# ---------------------------------------------------------------------------
# high-level operations (used by the MCP tools and the CLI)


def agent_status(target, cdir=None):
    """Registered agent in the foreground of the exact pane, prompt state and pane info."""
    info = pane_agent(target)
    agent = info['agent']
    raw = tmux.capture_pane(info['pane_id'], escapes=True)
    screen = strip_ansi(raw)
    det = detector_for(agent)
    panes = tmux.list_panes(info['pane_id'])
    pane = next((p for p in panes if p.get('pane_id') == info['pane_id']), panes[0] if panes else {})
    return {
        'target': target,
        'pane_id': info['pane_id'],
        'agent': agent,
        'typeable': agent is not None,
        'prompt_blank': det(input_view(raw)) if det else None,
        'supported': det is not None,
        'current_command': pane.get('current_command'),
        'current_path': pane.get('current_path'),
        'screen_sha256': screen_hash(screen),
        'last_lines': [l for l in screen.rstrip('\n').split('\n')][-5:],
    }


def pane_ready(target, probe=False, agent=None, cdir=None):
    """Is the agent's prompt empty?  With probe=True use the space probe
    (sends a space and a backspace) to see past placeholder text; probing only
    ever types into a pane running a registered agent."""
    if probe:
        info = require_agent_pane(target)
    else:
        info = pane_agent(target)
    agent = agent or info['agent']
    target = info['pane_id']
    tui = tui_for(agent, target)
    if tui is None:
        return {'target': target, 'agent': agent, 'ready': None,
                'note': f"no prompt detector for agent {agent!r}"}
    ready = tui.ensure_prompt_is_empty() if probe else tui.is_prompt_blank()
    return {'target': target, 'agent': agent, 'ready': bool(ready), 'probed': probe}


def tell_agent(target, text, new_conversation=False, require_empty_prompt=True,
               agent=None, cdir=None):
    """tell-worker's send sequence for an arbitrary pane.

    1. (optional) reach an empty prompt via the space probe; refuse otherwise
    2. (optional) '/new', 500ms, Enter, wait 10s
    3. type text literally, 500ms, Enter
    """
    info = require_agent_pane(target)          # never type into a shell
    agent = (agent or info['agent']).lower()
    pane = info['pane_id']
    tui = tui_for(agent, pane)
    if tui is None:
        tui = TuiAgent(pane)
        if require_empty_prompt:
            raise AgentNotReady(f"no prompt detector for agent {agent!r}; "
                                "pass require_empty_prompt=false to send anyway")
    elif require_empty_prompt and not tui.ensure_prompt_is_empty():
        raise AgentNotReady(f"{target}: never reached an empty prompt (the user may be typing); nothing sent")
    if new_conversation:
        tui.new_conversation()
    tui.send_text(text)
    return {'target': target, 'pane_id': pane, 'agent': agent, 'sent_chars': len(text),
            'new_conversation': bool(new_conversation)}


def wait_for_idle(target, timeout=120.0, poll=1.0, settle=3.0, agent=None, cdir=None,
                  sleep=time.sleep, capture=None, clock=time.monotonic):
    """Block until the agent looks idle: the screen is unchanged for `settle`
    seconds and (when the agent has a detector) its prompt is blank."""
    if capture is None:
        info = pane_agent(target)
        agent = agent or info['agent']
        pane = info['pane_id']
        capture = lambda: input_view(tmux.capture_pane(pane, escapes=True))  # noqa: E731
    det = detector_for(agent)
    start = clock()
    last_hash, stable_since = None, start
    screen = ''
    while True:
        screen = capture()
        h = screen_hash(screen)
        now = clock()
        if h != last_hash:
            last_hash, stable_since = h, now
        blank = det(screen) if det else True
        if blank and now - stable_since >= settle:
            return {'target': target, 'agent': agent, 'idle': True,
                    'waited_sec': round(now - start, 2), 'prompt_blank': det(screen) if det else None}
        if now - start >= timeout:
            return {'target': target, 'agent': agent, 'idle': False,
                    'waited_sec': round(now - start, 2), 'prompt_blank': det(screen) if det else None,
                    'last_lines': screen.rstrip('\n').split('\n')[-5:]}
        sleep(poll)
