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
same window reuses that shell only if it is idle at an empty prompt in the same worktree.

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
PROMPT_RE = re.compile(r'[$#]\s*$')
SHELL_STATE_FORMAT = '#{pane_id}\t#{pane_current_command}\t#{window_panes}\t#{cursor_y}\t#{pane_current_path}'


def launch_line(window, argv):
    """The exact line typed into the agent's shell: argv in its own scope in the slice."""
    return shlex.join(tmux.scope_argv(window, argv))


def shell_problem(target, cwd, run):
    """None if target is an idle bash at an empty prompt in cwd (alone in its window),
    else the reason it is not."""
    try:
        out = run([tmux.TMUX, 'display-message', '-p', '-t', target, SHELL_STATE_FORMAT]).stdout
        parts = out.rstrip('\n').split('\t', 4)
        if len(parts) != 5:
            return f"cannot read the state of {target}"
        pane, cmd, npanes, y, path = parts
        if npanes != '1':
            return f"{npanes} panes in the window"
        if cmd not in SHELL_NAMES:
            return f"busy: foreground is {cmd or 'unknown'}, not an idle bash"
        if os.path.realpath(path) != os.path.realpath(cwd):
            return f"shell is in {path}, not {cwd}"
        lines = run([tmux.TMUX, 'capture-pane', '-p', '-t', pane]).stdout.split('\n')
    except tmux.TmuxError as e:
        return f"{target}: {e.stderr or 'no such pane'}"
    line = lines[int(y)] if y.isdigit() and int(y) < len(lines) else ''
    if not PROMPT_RE.search(line):
        return f"shell prompt is not empty ({line.strip()[-40:]!r})"
    return None


def launch_agent(agent, cwd, session='agents', window_name=None, run=None, shell_pane=None,
                 sleep=time.sleep, wait=SHELL_WAIT):
    """Start a registered agent in a new bash window of `session` (created if missing) in
    cwd, or in shell_pane, an existing idle shell there, by typing its fixed launch line.
    Raises AgentError, leaving any shell open, if the shell is busy, elsewhere or not ready.
    Internal: callers go through worktrees.spawn_agent, which gives every agent its own
    worktree and never starts one in the plain checkout."""
    argv = launch_argv(agent)
    real = validate_cwd(cwd)
    validate_session(session)
    name = validate_window_name(window_name) if window_name else default_window_name(agent, real)
    run = run or tmux.run_checked
    if shell_pane is None:
        if tmux.has_session(session):
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
        why = shell_problem(target, real, run)
        while why and time.monotonic() < deadline:
            sleep(0.2)
            why = shell_problem(target, real, run)
    else:
        target, info = shell_pane, {'pane_id': shell_pane}
        why = shell_problem(target, real, run)
    if why:
        raise AgentError(f"not starting {agent} in {target} ({name}): {why}; the shell is left open")
    line = launch_line(name, argv)
    run([tmux.TMUX, 'send-keys', '-t', target, '-l', '--', line])
    sleep(tmux.ENTER_DELAY)
    run([tmux.TMUX, 'send-keys', '-t', target, 'Enter'])
    return {'agent': agent, 'argv': argv, 'launch_line': line, 'cwd': real, 'session': session,
            'window_name': name, 'pane': info, 'shell_reused': shell_pane is not None}


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
