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
import subprocess
import time

from . import tmux
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


PROMPT_DETECTORS = {
    'claude': claude_prompt_blank,
    'codex': codex_prompt_blank,
    'gemini': gemini_prompt_blank,
}


def detector_for(agent):
    """Map a detected agent name to its prompt detector (codex matches by
    substring, as in tell-worker.mts)."""
    a = (agent or '').lower()
    if 'codex' in a:
        return codex_prompt_blank
    return PROMPT_DETECTORS.get(a)


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
        self._capture = capture or (lambda t: tmux.capture_pane(t))
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


def tui_for(agent, target, **kw):
    """TUI wrapper for a detected agent name, or None if unsupported."""
    a = (agent or '').lower()
    if 'codex' in a:
        return CodexTui(target, **kw)
    if a == 'claude':
        return ClaudeTui(target, **kw)
    if a == 'gemini':
        return GeminiTui(target, **kw)
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
# high-level operations (used by the MCP tools and the CLI)

class AgentNotReady(RuntimeError):
    pass


def agent_status(target, cdir=None):
    """Detected agent, prompt state and pane info for a pane target."""
    agent = detect_agent(target, cdir=cdir)
    screen = tmux.capture_pane(target)
    det = detector_for(agent)
    panes = tmux.list_panes(target)
    pane = panes[0] if panes else {}
    if '.' in target.rsplit(':', 1)[-1]:
        idx = target.rsplit('.', 1)[-1]
        pane = next((p for p in panes if str(p.get('pane_index')) == idx), pane)
    return {
        'target': target,
        'agent': agent,
        'prompt_blank': det(screen) if det else None,
        'supported': det is not None,
        'current_command': pane.get('current_command'),
        'current_path': pane.get('current_path'),
        'screen_sha256': screen_hash(screen),
        'last_lines': [l for l in screen.rstrip('\n').split('\n')][-5:],
    }


def pane_ready(target, probe=False, agent=None, cdir=None):
    """Is the agent's prompt empty?  With probe=True use the space probe
    (sends a space and a backspace) to see past placeholder text."""
    agent = agent or detect_agent(target, cdir=cdir)
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
    agent = (agent or detect_agent(target, cdir=cdir) or 'claude').lower()
    tui = tui_for(agent, target)
    if tui is None:
        tui = TuiAgent(target)
        if require_empty_prompt:
            raise AgentNotReady(f"no prompt detector for agent {agent!r}; "
                                "pass require_empty_prompt=false to send anyway")
    elif require_empty_prompt and not tui.ensure_prompt_is_empty():
        raise AgentNotReady(f"{target}: never reached an empty prompt (the user may be typing); nothing sent")
    if new_conversation:
        tui.new_conversation()
    tui.send_text(text)
    return {'target': target, 'agent': agent, 'sent_chars': len(text),
            'new_conversation': bool(new_conversation)}


def wait_for_idle(target, timeout=120.0, poll=1.0, settle=3.0, agent=None, cdir=None,
                  sleep=time.sleep, capture=None, clock=time.monotonic):
    """Block until the agent looks idle: the screen is unchanged for `settle`
    seconds and (when the agent has a detector) its prompt is blank."""
    agent = agent or detect_agent(target, cdir=cdir)
    det = detector_for(agent)
    capture = capture or (lambda: tmux.capture_pane(target))
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
