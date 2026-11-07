"""Small wrappers around the tmux command line.

Every call passes an argv list to subprocess, never a shell string. The MCP server
uses the structured helpers (list_sessions, list_panes, capture_pane,
send_keys_literal, new_session, new_window). The swarm CLI uses the older helpers.
"""
import os
import re
import subprocess
import time
import uuid

TMUX = 'tmux'

# Field separator for -F formats. A tab is not legal in session names
# created by swarm, and tmux will not put one in the numeric fields.
SEP = '\t'

DEFAULT_SHELL = "${SHELL:-bash}"

# send-keys writes straight to the pane and never goes through tmux's key tables, so a
# prefix key sent with it never reaches tmux: the program in the pane gets a stray
# control character. Clients use tangentswarm.tmux_actions for tmux commands instead,
# and send_keys refuses the prefix keys (and C-b, Claude Code's "run in background").

def prefix_keys(target):
    """The prefix keys (the prefix and prefix2 options) in effect for `target`'s session:
    its own setting if it overrides them, else the global one. [] if tmux can't say."""
    try:
        r = _run([TMUX, 'display-message', '-p', '-t', target, '#{prefix}\t#{prefix2}'])
    except OSError:
        return []
    if r.returncode != 0:
        return []
    return [k for k in r.stdout.strip('\n').split('\t') if k.strip() and k.strip() != 'None']


def _key(name):
    """Normalise a key name for comparison: case-insensitive, ^R spelled C-r."""
    k = name.strip().lower()
    return 'c-' + k[1] if len(k) == 2 and k[0] == '^' else k


def refused_key(keys, target, literal=False):
    """Why send_keys should not send `keys` to `target`, or None. 'ctrl_b': a raw 0x02 byte,
    or C-b as a key name. 'prefix': a prefix key of the target's session as a key name (only
    checked when literal=False, since literal text can't press one)."""
    if '\x02' in keys or (not literal and _key(keys) == 'c-b'):
        return 'ctrl_b'
    if not literal and _key(keys) in {_key(p) for p in prefix_keys(target)}:
        return 'prefix'
    return None


class TmuxError(RuntimeError):
    """Raised when a structured tmux helper fails."""

    def __init__(self, argv, returncode, stderr):
        self.argv = list(argv)
        self.returncode = returncode
        self.stderr = (stderr or '').strip()
        super().__init__(f"{' '.join(self.argv)} failed ({returncode}): {self.stderr}")


# Every pane this package creates runs its command in its own transient systemd scope in
# swarm-workers.slice, so the slice's memory cap covers it. The pane COMMAND is wrapped,
# never the tmux client: a client only asks the server, which forks the pane outside any
# scope the client is in. Every tmux call goes through _run, which applies scope_spawn.
SLICE = 'swarm-workers.slice'
# subcommand -> (flags that take a value, flags naming the window for the unit, in order)
SPAWN_COMMANDS = {
    'new-session': ('cefFnstxy', 'ns'), 'new-window': ('ceFnt', 'n'),
    'split-window': ('ceFlpt', 't'), 'respawn-pane': ('cet', 't'), 'respawn-window': ('cet', 't'),
}
SPAWN_ALIASES = {'new': 'new-session', 'neww': 'new-window', 'splitw': 'split-window',
                 'respawnp': 'respawn-pane', 'respawnw': 'respawn-window'}
GLOBAL_VALUE_FLAGS = {'-c', '-f', '-L', '-S', '-T'}
DEFAULT_PANE_SHELL = 'exec "${SHELL:-/bin/sh}" -l'


def unit_name(window):
    slug = re.sub(r'[^A-Za-z0-9_-]+', '-', window or '').strip('-')[:40] or 'pane'
    return f"swarm-{slug}-{uuid.uuid4().hex[:12]}"


def scope_argv(window, argv):
    """argv run in its own scope in SLICE. systemd-run --scope execs argv in place, so the
    pane's process is still the agent itself and agent detection sees it unchanged."""
    return ['/usr/bin/env', f'XDG_RUNTIME_DIR=/run/user/{os.getuid()}',
            'systemd-run', '--user', f'--slice={SLICE}', '--scope', '--expand-environment=no',
            f'--unit={unit_name(window)}', '--', *argv]


def _split_flags(args, value_flags):
    """Split a tmux subcommand's args into (flag args, {flag: value}, command args)."""
    i, values = 0, {}
    while i < len(args):
        a = args[i]
        if a == '--':
            return args[:i + 1], values, args[i + 1:]
        if not a.startswith('-') or a == '-':
            break
        for j, ch in enumerate(a[1:], 1):
            if ch in value_flags:
                if j + 1 < len(a):
                    values[ch] = a[j + 1:]
                else:
                    i += 1
                    values[ch] = args[i] if i < len(args) else ''
                break
        i += 1
    return args[:i], values, args[i:]


# The escapes tmux's args_escape (cmd_stringify_argv) writes into pane_start_command.
START_ESCAPES = {'\\': '\\', '"': '"', "'": "'", '$': '$', '~': '~', '#': '#', ';': ';',
                 '{': '{', '}': '}', '%': '%', ' ': ' ', 'n': '\n', 't': '\t', 'r': '\r'}


def _unescape_start(s, i, end_quote):
    """Decode one quoted or bare piece of a start command from s[i]; return (text, next i)."""
    out = []
    while i < len(s):
        c = s[i]
        if c == end_quote or (end_quote is None and c in ' "\''):
            return ''.join(out), i
        if c == '\\':
            nxt = s[i + 1:i + 2]
            if re.match(r'\\\$[A-Za-z_{]', s[i + 1:i + 4]):      # tmux writes $NAME as \\$NAME
                out.append('$')
                i += 3
                continue
            if re.fullmatch(r'[0-7]', nxt) and re.fullmatch(r'[0-7]{3}', s[i + 1:i + 4]):
                out.append(chr(int(s[i + 1:i + 4], 8)))
                i += 4
                continue
            if nxt not in START_ESCAPES:
                raise ValueError(f"unknown escape \\{nxt} in start command")
            out.append(START_ESCAPES[nxt])
            i += 2
            continue
        out.append(c)
        i += 1
    if end_quote is not None:
        raise ValueError('unterminated quote in start command')
    return ''.join(out), i


def parse_start_command(s):
    """The exact argv of a #{pane_start_command} (tmux's own quoting). Raises ValueError on
    anything it cannot decode exactly, so a respawn is refused rather than run altered."""
    argv, i = [], 0
    while i < len(s):
        if s[i] == ' ':
            i += 1
            continue
        word = []
        while i < len(s) and s[i] != ' ':
            if s[i] in '"\'':
                text, i = _unescape_start(s, i + 1, s[i])
                i += 1
            else:
                text, i = _unescape_start(s, i, None)
            word.append(text)
        argv.append(''.join(word))
    return argv


def _unscoped(argv):
    """argv without the scope wrapper a pane we started already has."""
    if argv[:3] == ['/usr/bin/env', argv[1] if len(argv) > 1 else '', 'systemd-run'] \
            and argv[1].startswith('XDG_RUNTIME_DIR=') and f'--slice={SLICE}' in argv:
        return argv[argv.index('--', argv.index(f'--slice={SLICE}')) + 1:]
    return argv


def _scoped_respawn(prefix, args, flags, values, window):
    """A commandless respawn reruns the pane's start command, which may predate scoping.
    Read it, decode tmux's quoting back to the exact argv, drop an older scope wrapper and
    run it in a fresh scope. Refuses (TmuxError) rather than rerun it altered or unscoped."""
    t = values.get('t')
    r = subprocess.run([*prefix, 'display-message', '-p', *(['-t', t] if t else []),
                        '#{pane_start_command}'], stdin=subprocess.DEVNULL,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    if r.returncode != 0:
        raise TmuxError(r.args, r.returncode, r.stderr)
    try:
        command = _unscoped(parse_start_command(r.stdout.rstrip('\n')))
    except ValueError as e:
        raise TmuxError(r.args, 1, f"cannot scope the respawn ({e}); pass the pane command explicitly, e.g. respawn-pane -k -t <pane> -- <command>") from e
    if not command:
        command = ['/bin/sh', '-c', DEFAULT_PANE_SHELL]
    elif len(command) == 1:
        command = ['/bin/sh', '-c', command[0]]
    return [args[0], *(f for f in flags if f != '--'), '--', *scope_argv(window, command)]


def _scope_one(args, prefix=('tmux',)):
    sub = SPAWN_ALIASES.get(args[0], args[0]) if args else None
    if sub not in SPAWN_COMMANDS:
        return args
    value_flags, name_flags = SPAWN_COMMANDS[sub]
    flags, values, command = _split_flags(args[1:], value_flags)
    window = next((values[f] for f in name_flags if values.get(f)), sub)
    if not command and sub.startswith('respawn'):
        return _scoped_respawn(list(prefix), args, flags, values, window)
    if not command:
        command = ['/bin/sh', '-c', DEFAULT_PANE_SHELL]
    elif len(command) == 1:
        command = ['/bin/sh', '-c', command[0]]    # tmux runs a single argument with sh -c too
    if flags[-1:] != ['--']:
        flags = flags + ['--']
    return [args[0], *flags, *scope_argv(window, command)]


def scope_spawn(argv):
    """Rewrite a tmux argv so every command in it that starts a pane runs scoped."""
    if not argv or os.path.basename(argv[0]) != 'tmux':
        return argv
    i = 1
    while i < len(argv) and argv[i].startswith('-') and argv[i] != '--':
        i += 2 if argv[i] in GLOBAL_VALUE_FLAGS else 1
    prefix = argv[:i]
    out, cur = list(prefix), []
    for a in argv[i:]:
        if a in (';', '\\;'):
            out += _scope_one(cur, prefix) + [a]
            cur = []
        else:
            cur.append(a)
    return out + _scope_one(cur, prefix)


def _run(argv, check=False):
    """Run tmux with an argv list, capturing text output. Panes it creates run scoped."""
    argv = scope_spawn(argv)
    result = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, check=False)
    if check and result.returncode != 0:
        raise TmuxError(argv, result.returncode, result.stderr)
    return result


# ---------------------------------------------------------------------------
# -F format parsing

SESSION_FIELDS = [
    ('name', 'session_name', str),
    ('id', 'session_id', str),
    ('windows', 'session_windows', int),
    ('attached', 'session_attached', int),
    ('created', 'session_created', int),
]

PANE_FIELDS = [
    ('session', 'session_name', str),
    ('window_index', 'window_index', int),
    ('window_name', 'window_name', str),
    ('pane_index', 'pane_index', int),
    ('pane_id', 'pane_id', str),
    ('active', 'pane_active', bool),
    ('window_active', 'window_active', bool),
    ('current_command', 'pane_current_command', str),
    ('width', 'pane_width', int),
    ('height', 'pane_height', int),
    ('pid', 'pane_pid', int),
    ('dead', 'pane_dead', bool),
    # Keep the path last. parse_format_output lets the last field take up
    # any stray separators.
    ('current_path', 'pane_current_path', str),
]


def build_format(fields):
    """Build a tmux -F format string from a field table."""
    return SEP.join('#{%s}' % var for _, var, _ in fields)


def _convert(value, typ):
    if typ is str:
        return value
    if typ is bool:
        return value.strip() not in ('', '0')
    try:
        return typ(value)
    except (TypeError, ValueError):
        return None


def parse_format_output(text, fields):
    """Parse tmux output produced with build_format(fields) into dicts.

    Values are split on SEP. The last field takes up any extra separators, so
    a stray tab in a path cannot shift the other columns.
    """
    rows = []
    n = len(fields)
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split(SEP, n - 1)
        if len(parts) < n:
            parts += [''] * (n - len(parts))
        rows.append({key: _convert(val, typ) for (key, _, typ), val in zip(fields, parts)})
    return rows


SESSION_FORMAT = build_format(SESSION_FIELDS)
PANE_FORMAT = build_format(PANE_FIELDS)


def _no_server(stderr):
    s = (stderr or '').lower()
    return 'no server running' in s or 'error connecting to' in s or 'no sessions' in s


# ---------------------------------------------------------------------------
# Structured helpers (used by the MCP server and the CLI)

def list_sessions():
    """Return a list of dicts: name, id, windows, attached, created (epoch secs).

    Returns [] when no tmux server is running.
    """
    argv = [TMUX, 'list-sessions', '-F', SESSION_FORMAT]
    result = _run(argv)
    if result.returncode != 0:
        if _no_server(result.stderr):
            return []
        raise TmuxError(argv, result.returncode, result.stderr)
    return parse_format_output(result.stdout, SESSION_FIELDS)


def list_panes(target=None, all=False):
    """Return a list of pane dicts.

    target is a session or window target, such as 'agents' or 'agents:1'. A
    session target lists all panes in all its windows.
    all=True lists every pane on the server and ignores target.
    """
    argv = [TMUX, 'list-panes']
    if all:
        argv.append('-a')
    elif target:
        # A bare session name ("agents") means every window in it; a window
        # target ("agents:1") means just that window.
        if ':' not in target and '.' not in target:
            argv.append('-s')
        argv += ['-t', target]
    argv += ['-F', PANE_FORMAT]
    result = _run(argv)
    if result.returncode != 0:
        if all and _no_server(result.stderr):
            return []
        raise TmuxError(argv, result.returncode, result.stderr)
    return parse_format_output(result.stdout, PANE_FIELDS)


def capture_pane(target, history_lines=None, escapes=False):
    """Return the visible text of a pane (plus history_lines of scrollback).

    escapes=True passes -e so colour/attribute escape sequences are kept;
    the default is plain text.
    """
    argv = [TMUX, 'capture-pane', '-p', '-t', target]
    if escapes:
        argv.append('-e')
    if history_lines:
        argv += ['-S', f'-{int(history_lines)}']
    return _run(argv, check=True).stdout


# Pause between typing literal text and pressing Enter. TUIs (Claude Code,
# Muse, etc.) can drop an Enter that arrives in the same burst as the text.
ENTER_DELAY = 0.5


def send_keys_literal(target, text, enter=True, enter_delay=ENTER_DELAY):
    """Type text literally into a pane (send-keys -l), then press Enter
    in a separate send-keys call so the text is never interpreted as keys.
    Waits enter_delay seconds before the Enter."""
    _run([TMUX, 'send-keys', '-t', target, '-l', '--', text], check=True)
    if enter:
        if enter_delay:
            time.sleep(enter_delay)
        _run([TMUX, 'send-keys', '-t', target, 'Enter'], check=True)


def run_checked(argv):
    """Run tmux with an argv list; raise TmuxError on a non-zero exit."""
    return _run(argv, check=True)


def display(target, fmt):
    """Expand a -F format for the pane `target` resolves to, which is the pane send-keys would
    use. Raises TmuxError if the target does not exist."""
    return _run([TMUX, 'display-message', '-p', '-t', target, fmt], check=True).stdout.rstrip('\n')


def _created_info(result):
    row = parse_format_output(result.stdout, PANE_FIELDS)
    return row[0] if row else {}


def new_session(name, cwd=None, command=None):
    """Create a detached session. Returns the new pane's info dict.

    tmux gets command as a single shell-command argument and runs it with the
    default shell. Leave it out to start the default shell.
    Raises TmuxError on failure.
    """
    argv = [TMUX, 'new-session', '-d', '-P', '-F', PANE_FORMAT, '-s', name]
    if cwd:
        argv += ['-c', cwd]
    if command:
        argv.append(command)
    return _created_info(_run(argv, check=True))


def new_window(session, name=None, cwd=None, command=None):
    """Create a window in session at the next free index, without switching to it.
    Returns the new pane's info dict. Raises TmuxError on failure."""
    argv = [TMUX, 'new-window', '-d', '-P', '-F', PANE_FORMAT, '-t', f'{session}:']
    if name:
        argv += ['-n', name]
    if cwd:
        argv += ['-c', cwd]
    if command:
        argv.append(command)
    return _created_info(_run(argv, check=True))


# ---------------------------------------------------------------------------
# Older helpers used by the swarm CLI

def has_session(session_name):
    """Check if a tmux session exists."""
    try:
        return _run([TMUX, 'has-session', '-t', session_name]).returncode == 0
    except Exception:
        return False


def rename_window(target, name):
    """Rename a tmux window."""
    return subprocess.run([TMUX, 'rename-window', '-t', target, name])


def send_keys(target, keys, enter=True):
    """Send keys to a tmux pane. tmux interprets key names such as C-c.
    With enter=True, press Enter at the end."""
    cmd = [TMUX, 'send-keys', '-t', target, keys]
    if enter:
        cmd.append('Enter')
    return subprocess.run(cmd)


def run_command(command):
    """Run a command directly without a shell."""
    return subprocess.run(command.split(), stderr=subprocess.PIPE, stdout=subprocess.PIPE, text=True, check=False)


def run_tmux_command(session_name, command):
    """Run a tmux command against the specified session."""
    cmd = [TMUX] + command.split()
    # Add the target session if it's not already specified
    if '-t' not in cmd:
        cmd.extend(['-t', session_name])
    return _run(cmd)


def next_window(session_name):
    """Switch to the next window in a session."""
    return subprocess.run([TMUX, 'next-window', '-t', session_name])


def split_window(target, split_type, directory, shell=DEFAULT_SHELL):
    """Split a tmux window.
    split_type is '-h' for a horizontal split or '-v' for a vertical split."""
    return _run([TMUX, 'split-window', split_type, '-t', target, '-c', directory, shell])


def select_pane(target):
    """Select a tmux pane."""
    return subprocess.run([TMUX, 'select-pane', '-t', target])


def kill_pane(target):
    """Kill a tmux pane. The CLI uses this, and MCP does not expose it."""
    return subprocess.run([TMUX, 'kill-pane', '-t', target], check=False)


def switch_client(target):
    """Switch tmux client to the specified session."""
    return subprocess.run([TMUX, 'switch-client', '-t', target])


def attach_session(session_name, unicode=True):
    """Attach to a tmux session."""
    cmd = [TMUX]
    if unicode:
        cmd.append('-u')
    cmd.extend(['attach-session', '-t', session_name])
    return cmd  # return the argv for os.execvp()


def new_window_args(*args):
    """Create a window from raw tmux new-window arguments, for the CLI.
    Returns the CompletedProcess."""
    return _run([TMUX, 'new-window', *args])


def kill_session(*args):
    """Kill a tmux session. The CLI uses this, and MCP does not expose it."""
    cmd = [TMUX, 'kill-session']
    cmd.extend(args)
    return subprocess.run(cmd, stderr=subprocess.PIPE, stdout=subprocess.PIPE, text=True, check=False)
