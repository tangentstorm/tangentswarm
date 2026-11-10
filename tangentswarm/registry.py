"""The fixed set of coding agents swarm-mcp may start and type into.

swarm-mcp never runs a caller-supplied command. `start_agent` launches one of the
agents below in its own git worktree under the agent root (default ~/ver; see
worktrees.py). The typing tools (send_keys, tell_agent, the pane_ready probe and
tell_worker) only type into a pane whose foreground process is one of these agents,
never into a bare shell.

Every agent window is a persistent bash shell in its worktree (Michal's rule). start_agent
creates the window running /bin/bash -l (tmux scopes it like every pane, see tmux.py), waits
for an idle prompt in the right directory, then types ONE fixed launch line into it: the
shlex-quoted scope_argv() of the agent's absolute binary and fixed flags, so the agent runs
in its own swarm-workers.slice scope. It is typed with send-keys -l and a separate Enter;
nothing from the caller is ever in it. When the agent exits, the shell and directory
survive with the launch line in history (up-arrow to restart). A later start_agent for the
same window name reuses that window (found by session and name, wherever its shell is) only
if its shell is idle at an exact, empty prompt in the same worktree; the line is typed, read
back and only then submitted with Enter.

The operator, not the MCP caller, can set these overrides:
  TANGENTSWARM_AGENT_ROOT    project root agents may start in (default ~/ver)
  TANGENTSWARM_AGENT_<NAME>  absolute path of that agent's binary
"""
import os
import re
import shlex
import time
from dataclasses import dataclass, field
from typing import Callable, Literal

from . import tmux

AgentName = Literal['claude', 'muse', 'grok', 'gemini', 'codex']

# Interpreters whose first argument is the actual program (node /x/codex.js).
INTERPRETERS = {'node', 'nodejs', 'bun', 'deno'}


class AgentError(ValueError):
    """A launch or typing request that the agent policy refuses."""


@dataclass(frozen=True)
class AgentSpec:
    name: str
    candidates: tuple            # binary paths, with '~' expanded at lookup time
    flags: tuple = ()            # fixed flags; callers can never add any
    names: frozenset = field(default_factory=frozenset)       # exact process names
    prefixes: tuple = ()         # process-name prefixes (versioned binaries)
    adapter: str = 'none'        # 'full' (prompt detector verified), 'basic' or 'none'

    def matches(self, name):
        return name in self.names or any(name.startswith(p) for p in self.prefixes)


REGISTRY = {
    'claude': AgentSpec(
        'claude', ('~/.npm-global/bin/claude', '~/workspace/.npm-global/bin/claude', '~/.local/bin/claude',
                   '~/.claude/local/claude'),
        names=frozenset({'claude', 'claude.exe'}), adapter='full'),
    'muse': AgentSpec(
        # ~/.local/bin/muse is an auto-updating launcher that execs muse-bin-<version>
        'muse', ('~/.local/bin/muse',), flags=('--trust-workspace',),
        names=frozenset({'muse'}), prefixes=('muse-bin',), adapter='full'),
    'grok': AgentSpec(
        'grok', ('~/.grok/bin/grok',),
        names=frozenset({'grok'}), prefixes=('grok-linux',), adapter='none'),
    'gemini': AgentSpec(
        'gemini', ('~/.npm-global/bin/gemini', '~/workspace/.npm-global/bin/gemini', '~/.local/bin/gemini',
                   '/usr/local/bin/gemini', '/usr/bin/gemini'),
        names=frozenset({'gemini', 'gemini.js', 'agy'}), adapter='basic'),
    'codex': AgentSpec(
        'codex', ('~/.npm-global/bin/codex', '~/workspace/.npm-global/bin/codex', '/usr/local/bin/codex'),
        names=frozenset({'codex', 'codex.js'}), prefixes=('codex-',), adapter='basic'),
}

AGENT_NAMES = tuple(REGISTRY)


# ---------------------------------------------------------------------------
# binaries

def _executable(p):
    return os.path.isfile(p) and os.access(p, os.X_OK)


def resolve_binary(name):
    """Return the absolute path of an installed agent's binary, or None."""
    spec = REGISTRY[name]
    override = os.environ.get(f'TANGENTSWARM_AGENT_{name.upper()}')
    cands = (override,) if override else spec.candidates
    for c in cands:
        p = os.path.expanduser(c)
        if os.path.isabs(p) and _executable(p):
            return p
    return None


def _exec_argv(path, flags):
    # tmux execs a multi-argument command directly but hands a single argument to
    # `sh -c`; run flag-less agents through env so a shell is never involved.
    return [path, *flags] if flags else ['/usr/bin/env', '--', path]


def launch_argv(name):
    """Return the exact argv start_agent runs for an agent. Raises AgentError."""
    if name not in REGISTRY:
        raise AgentError(f"unknown agent {name!r}; allowed: {', '.join(AGENT_NAMES)}")
    path = resolve_binary(name)
    if path is None:
        raise AgentError(f"agent {name!r} is not installed on this host")
    return _exec_argv(path, REGISTRY[name].flags)


def registry_info():
    """Return [{name, installed, path, argv, adapter}] for docs and tools."""
    out = []
    for name, spec in REGISTRY.items():
        path = resolve_binary(name)
        out.append({'name': name, 'installed': path is not None, 'path': path,
                    'argv': _exec_argv(path, spec.flags) if path else None, 'adapter': spec.adapter})
    return out


# ---------------------------------------------------------------------------
# argument validation

SESSION_RE = re.compile(r'^[A-Za-z0-9_-]{1,40}$')
WINDOW_RE = re.compile(r'^[a-z0-9]+(?:-[a-z0-9]+)*$')


def agent_root():
    return os.path.realpath(os.path.expanduser(os.environ.get('TANGENTSWARM_AGENT_ROOT', '~/ver')))


def validate_cwd(cwd):
    """Resolve cwd (absolute, or relative to the agent root) to a real directory strictly
    inside the agent root. Symlinks are resolved first, so they cannot lead outside it."""
    if not isinstance(cwd, str) or not cwd.strip() or any(c in cwd for c in '\0\n\r'):
        raise AgentError('cwd must be a directory under ' + agent_root())
    root = agent_root()
    p = os.path.expanduser(cwd.strip())
    if not os.path.isabs(p):
        p = os.path.join(root, p)
    real = os.path.realpath(p)
    if os.path.commonpath([root, real]) != root or real == root:
        raise AgentError(f"cwd {cwd!r} is not inside {root}/ (resolved to {real})")
    if not os.path.isdir(real):
        raise AgentError(f"cwd {cwd!r} is not an existing directory")
    return real


def validate_session(session):
    if not isinstance(session, str) or not SESSION_RE.match(session):
        raise AgentError(f"session {session!r} must match {SESSION_RE.pattern}")
    return session


def validate_window_name(name):
    if not isinstance(name, str) or len(name) > 40 or not WINDOW_RE.match(name):
        raise AgentError(f"window name {name!r} must be kebab-case (a-z, 0-9, '-'), max 40 chars")
    return name


def default_window_name(agent, cwd):
    slug = re.sub(r'[^a-z0-9]+', '-', os.path.basename(cwd).lower()).strip('-')
    return (f"{agent}-{slug}" if slug else agent)[:40].strip('-')


# ---------------------------------------------------------------------------
# starting an agent

SHELL_ARGV = ('/bin/bash', '-l')
SHELL_NAMES = frozenset({'bash'})
SHELL_WAIT = 15.0
ECHO_WAIT = 3.0
# An empty input buffer is only accepted when the cursor's logical line is exactly a known
# prompt with the cursor right after it. The known prompt is the one recorded in the pane
# option PROMPT_OPTION when start_agent created the shell, or else one of PROMPT_TAILS.
# Anything else (typed text, PS2 '> ', a search prompt, a changed prompt) is refused.
PROMPT_TAILS = ('$ ', '# ')
PROMPT_OPTION = '@swarm_prompt'
SHELL_FIELDS = ('pane_id', 'pane_current_command', 'window_panes', 'history_size', 'pane_height',
                'cursor_y', 'cursor_x', 'pane_current_path')
SHELL_STATE_FORMAT = '\t'.join('#{%s}' % f for f in SHELL_FIELDS)


def launch_line(window, argv):
    """The exact line typed into the agent's shell: argv in its own scope in the slice."""
    return shlex.join(tmux.scope_argv(window, argv))


def shell_state(target, run):
    """The pane's shell fields (see SHELL_FIELDS), with numbers as ints. Raises TmuxError."""
    out = run([tmux.TMUX, 'display-message', '-p', '-t', target, SHELL_STATE_FORMAT]).stdout
    parts = out.rstrip('\n').split('\t', len(SHELL_FIELDS) - 1)
    if len(parts) != len(SHELL_FIELDS):
        raise tmux.TmuxError(['display-message', target], 1, f"cannot read the state of {target}")
    st = dict(zip(SHELL_FIELDS, parts))
    for k in ('window_panes', 'history_size', 'pane_height', 'cursor_y', 'cursor_x'):
        if not st[k].isdigit():
            raise tmux.TmuxError(['display-message', target], 1, f"bad {k} {st[k]!r} for {target}")
        st[k] = int(st[k])
    return st


def _logical_lines(run, pane, start, end):
    """Screen rows start..end (negative = history) as logical lines: wrapped rows joined."""
    out = run([tmux.TMUX, 'capture-pane', '-p', '-J', '-t', pane, '-S', str(start), '-E', str(end)]).stdout
    lines = out.split('\n')
    return lines[:-1] if lines and lines[-1] == '' else lines


def _prompt_row(run, st):
    """(row, why): the cursor's logical line and None, or None and why it is not a single
    logical line with nothing drawn below it."""
    pane, y = st['pane_id'], st['cursor_y']
    first = y - 1 if (y > 0 or st['history_size'] > 0) else y
    lines = _logical_lines(run, pane, first, y)
    below = _logical_lines(run, pane, y + 1, st['pane_height'] - 1) if y + 1 < st['pane_height'] else []
    if len(lines) != y - first + 1:
        return None, "the cursor row continues wrapped input; the input buffer is not empty"
    if any(l.strip() for l in below):
        return None, "text is drawn below the prompt; the input buffer is not empty"
    return lines[-1], None


def known_prompts(target, run):
    """The prompt recorded when start_agent created this shell, else PROMPT_TAILS."""
    out = run([tmux.TMUX, 'display-message', '-p', '-t', target, '#{%s}' % PROMPT_OPTION]).stdout
    out = out[:-1] if out.endswith('\n') else out
    return [out] if out else list(PROMPT_TAILS)


def _base_problem(st, cwd):
    if st['window_panes'] != 1:
        return f"{st['window_panes']} panes in the window"
    if st['pane_current_command'] not in SHELL_NAMES:
        return f"busy: foreground is {st['pane_current_command'] or 'unknown'}, not an idle bash"
    if os.path.realpath(st['pane_current_path']) != os.path.realpath(cwd):
        return f"shell is in {st['pane_current_path']}, not {cwd}"
    return None


def shell_problem(target, cwd, run):
    """None only if target is bash, alone in its window, in cwd, with a positively empty input
    buffer: the cursor row is a whole logical line (not the wrapped end of earlier input) that
    reads exactly the known prompt (the recorded one, else a PROMPT_TAILS entry), the cursor
    is right after it, and nothing is drawn below. Otherwise the reason. Fails closed."""
    try:
        st = shell_state(target, run)
        why = _base_problem(st, cwd)
        if why:
            return why
        row, why = _prompt_row(run, st)
        if why:
            return why
        known = known_prompts(target, run)
    except tmux.TmuxError as e:
        return f"{target}: {e.stderr or 'no such pane'}"
    if not any(row.rstrip(' ') == k.rstrip(' ') and st['cursor_x'] == len(k) for k in known):
        return (f"input buffer not positively empty (cursor line {row.strip()[-40:]!r}, column "
                f"{st['cursor_x']}; expected exactly {' or '.join(repr(k) for k in known)})")
    return None


def first_prompt_problem(target, cwd, run):
    """For a shell start_agent itself just created (detached, so nobody else has typed in it):
    None once bash shows its first prompt, which is then recorded as the pane's known prompt.
    The first prompt must be one logical line ending in '$ ' or '# ' with the cursor at its end."""
    try:
        st = shell_state(target, run)
        why = _base_problem(st, cwd)
        if why:
            return why
        row, why = _prompt_row(run, st)
        if why:
            return why
        prompt = (row + ' ' * st['cursor_x'])[:st['cursor_x']]
        if row.rstrip(' ') != prompt.rstrip(' ') or not prompt.endswith(PROMPT_TAILS):
            return f"no prompt yet (cursor line {row.strip()[-40:]!r}, column {st['cursor_x']})"
        run([tmux.TMUX, 'set-option', '-p', '-t', target, PROMPT_OPTION, prompt])
    except tmux.TmuxError as e:
        return f"{target}: {e.stderr or 'no such pane'}"
    return shell_problem(target, cwd, run)


def type_launch_line(target, line, run, sleep=time.sleep, wait=None):
    """Type line into a shell that shell_problem() just passed, then press Enter only after the
    echoed input buffer reads back as exactly prompt + line on one logical line ending at the
    cursor. Raises AgentError (Enter not pressed) otherwise."""
    st0 = shell_state(target, run)
    known = known_prompts(target, run)
    start_abs = st0['history_size'] + st0['cursor_y']
    run([tmux.TMUX, 'send-keys', '-t', target, '-l', '--', line])
    deadline = time.monotonic() + (ECHO_WAIT if wait is None else wait)
    sleep(tmux.ENTER_DELAY)
    while True:
        st = shell_state(target, run)
        got = None
        if st['pane_current_command'] in SHELL_NAMES and st['pane_id'] == st0['pane_id']:
            first = start_abs - st['history_size']
            lines = _logical_lines(run, target, first, st['cursor_y'])
            below = _logical_lines(run, target, st['cursor_y'] + 1, st['pane_height'] - 1) \
                if st['cursor_y'] + 1 < st['pane_height'] else []
            if len(lines) == 1 and not any(l.strip() for l in below):
                got = lines[0]
                if any(got.rstrip(' ') == (k + line).rstrip(' ') for k in known):
                    run([tmux.TMUX, 'send-keys', '-t', target, 'Enter'])
                    return
        if time.monotonic() >= deadline:
            raise AgentError(f"{target}: the input buffer does not read back as exactly the launch line "
                             f"({(got or '').strip()[-60:]!r}); Enter was NOT pressed")
        sleep(0.2)


def find_window_pane(session, name, run):
    """The first pane of the window of session named exactly name, or None. Raises AgentError
    if more than one window has that name, or if the windows cannot be listed (fails closed:
    an unknown answer must not lead to a second window)."""
    try:
        out = run([tmux.TMUX, 'list-panes', '-s', '-t', f'{session}:', '-F',
                   '#{window_id}\t#{window_name}\t#{pane_id}']).stdout
    except tmux.TmuxError as e:
        raise AgentError(f"cannot list the windows of {session} ({e.stderr}); not starting {name}") from e
    rows = [l.split('\t') for l in out.splitlines() if l.count('\t') == 2]
    wins = {}
    for wid, wname, pane in rows:
        if wname == name:
            wins.setdefault(wid, []).append(pane)
    if len(wins) > 1:
        raise AgentError(f"{len(wins)} windows in {session} are named {name}; not starting another")
    return next(iter(wins.values()))[0] if wins else None


def launch_agent(agent, cwd, session='agents', window_name=None, run=None, shell_pane=None,
                 sleep=time.sleep, wait=SHELL_WAIT):
    """Start a registered agent in the bash shell of window `window_name` of `session`, in cwd.
    An existing window of that name (or shell_pane) is reused only if shell_problem() passes;
    otherwise a new window (or the session) is created with a fresh bash. The fixed launch line
    is typed, and Enter pressed only after it reads back exactly (type_launch_line).
    Raises AgentError, leaving any shell open, if the shell is busy, elsewhere or not ready.
    Internal: callers go through worktrees.spawn_agent, which gives every agent its own
    worktree and never starts one in the plain checkout."""
    argv = launch_argv(agent)
    real = validate_cwd(cwd)
    validate_session(session)
    name = validate_window_name(window_name) if window_name else default_window_name(agent, real)
    run = run or tmux.run_checked
    has = tmux.has_session(session)
    if shell_pane is None and has:
        shell_pane = find_window_pane(session, name, run)
    if shell_pane is None:
        if has:
            cmd = [tmux.TMUX, 'new-window', '-d', '-P', '-F', tmux.PANE_FORMAT,
                   '-t', f'{session}:', '-n', name, '-c', real]
        else:
            cmd = [tmux.TMUX, 'new-session', '-d', '-P', '-F', tmux.PANE_FORMAT,
                   '-s', session, '-n', name, '-c', real]
        pane = tmux.parse_format_output(run(cmd + ['--', *SHELL_ARGV]).stdout, tmux.PANE_FIELDS)
        info = pane[0] if pane else {}
        target = info.get('pane_id')
        if not target:
            raise AgentError(f"tmux did not report the new shell pane for {name}")
        deadline = time.monotonic() + wait
        why = first_prompt_problem(target, real, run)
        while why and time.monotonic() < deadline:
            sleep(0.2)
            why = first_prompt_problem(target, real, run)
        reused = False
    else:
        target, info, reused = shell_pane, {'pane_id': shell_pane}, True
        why = shell_problem(target, real, run)
    if why:
        raise AgentError(f"not starting {agent} in {target} ({name}): {why}; the shell is left open")
    line = launch_line(name, argv)
    try:
        type_launch_line(target, line, run, sleep=sleep)
    except tmux.TmuxError as e:
        raise AgentError(f"{target}: {e.stderr or 'tmux failed'}; Enter was NOT pressed") from e
    return {'agent': agent, 'argv': argv, 'launch_line': line, 'cwd': real, 'session': session,
            'window_name': name, 'pane': info, 'shell_reused': reused}


# ---------------------------------------------------------------------------
# recognising a running agent

def process_names(comm, args):
    """Return the names a process goes by. These are its comm, argv[0]'s basename and, for
    interpreters, the script's basename (codex.js for node /x/codex.js)."""
    toks = (args or '').split(None, 2)
    names = {comm} if comm else set()
    if toks:
        names.add(os.path.basename(toks[0]))
    if (names & INTERPRETERS) and len(toks) > 1:
        names.add(os.path.basename(toks[1]))
    names.discard('')
    return names


def classify_process(comm, args):
    """Return the registered agent name for one process, or None."""
    names = process_names(comm, args)
    for spec in REGISTRY.values():
        if any(spec.matches(n) for n in names):
            return spec.name
    return None


def agent_from_processes(procs):
    """procs is [(pid, stat, comm, args)] for a pane's tty. Only processes in the terminal's
    foreground process group ('+' in stat) count, so a suspended agent under a shell
    prompt does not let anyone type into that shell."""
    for pid, stat, comm, args in procs:
        if '+' not in stat:
            continue
        a = classify_process(comm, args)
        if a:
            return a, pid
    return None, None
