"""The fixed set of coding agents swarm-mcp may start and type into.

swarm-mcp never runs a caller-supplied command. `start_agent` launches one of the
agents below in its own git worktree under the agent root (default ~/ver; see
worktrees.py). tmux runs
the absolute binary path and fixed flags directly, with no shell. The typing tools
(send_keys, tell_agent, the pane_ready probe and tell_worker) only type into a pane
whose foreground process is one of these agents, never into a bare shell.

The agent is the pane's own process, so the window closes when the agent exits and
leaves no shell prompt to type into.

The operator, not the MCP caller, can set these overrides:
  TANGENTSWARM_AGENT_ROOT    project root agents may start in (default ~/ver)
  TANGENTSWARM_AGENT_<NAME>  absolute path of that agent's binary
"""
import os
import re
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

def launch_agent(agent, cwd, session='agents', window_name=None, run=None):
    """Start a registered agent in a new window of `session` (created if missing), in cwd.
    The argv is passed to tmux as separate arguments, so tmux execs it without a shell.
    Internal: callers go through worktrees.spawn_agent, which gives every agent its own
    worktree and never starts one in the plain checkout."""
    argv = launch_argv(agent)
    real = validate_cwd(cwd)
    validate_session(session)
    name = validate_window_name(window_name) if window_name else default_window_name(agent, real)
    run = run or tmux.run_checked
    if tmux.has_session(session):
        cmd = [tmux.TMUX, 'new-window', '-d', '-P', '-F', tmux.PANE_FORMAT,
               '-t', f'{session}:', '-n', name, '-c', real]
    else:
        cmd = [tmux.TMUX, 'new-session', '-d', '-P', '-F', tmux.PANE_FORMAT,
               '-s', session, '-n', name, '-c', real]
    pane = tmux.parse_format_output(run(cmd + ['--', *argv]).stdout, tmux.PANE_FIELDS)
    info = pane[0] if pane else {}
    return {'agent': agent, 'argv': argv, 'cwd': real, 'session': session, 'window_name': name,
            'pane': info}


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
