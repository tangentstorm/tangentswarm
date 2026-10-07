"""Thin wrappers around the tmux command line.

Every call passes an argv list to subprocess (never a shell string).
The structured helpers (list_sessions, list_panes, capture_pane,
send_keys_literal, new_session, new_window) are what the MCP server uses;
the older helpers are kept for the swarm CLI.
"""
import subprocess
import time

TMUX = 'tmux'

# Field separator for -F formats. A tab is not legal in session names
# created by swarm, and tmux will not put one in the numeric fields.
SEP = '\t'

DEFAULT_SHELL = "${SHELL:-bash}"


class TmuxError(RuntimeError):
    """Raised when a structured tmux helper fails."""

    def __init__(self, argv, returncode, stderr):
        self.argv = list(argv)
        self.returncode = returncode
        self.stderr = (stderr or '').strip()
        super().__init__(f"{' '.join(self.argv)} failed ({returncode}): {self.stderr}")


def _run(argv, check=False):
    """Run tmux with an argv list, capturing text output."""
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
    # keep the path last: parse_format_output lets the last field absorb
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

    Values are split on SEP; the last field absorbs any extra separators so
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

    target: a session or window target (e.g. 'agents' or 'agents:1').
            With a session target, all panes in all its windows are listed.
    all:    list every pane on the server (target is ignored).
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


def _created_info(result):
    row = parse_format_output(result.stdout, PANE_FIELDS)
    return row[0] if row else {}


def new_session(name, cwd=None, command=None):
    """Create a detached session. Returns the new pane's info dict.

    command is passed to tmux as a single shell-command argument (tmux runs
    it with the default shell); omit it to start the default shell.
    Raises TmuxError on failure.
    """
    argv = [TMUX, 'new-session', '-d', '-P', '-F', PANE_FORMAT, '-s', name]
    if cwd:
        argv += ['-c', cwd]
    if command:
        argv.append(command)
    return _created_info(_run(argv, check=True))


def new_window(session, name=None, cwd=None, command=None):
    """Create a window in session (at the next free index, without switching
    to it). Returns the new pane's info dict. Raises TmuxError on failure."""
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
    """Send keys to a tmux pane (key names like C-c are interpreted).
    If enter=True, adds an 'Enter' key press at the end."""
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
    return subprocess.run(cmd, stderr=subprocess.PIPE, stdout=subprocess.PIPE, text=True, check=False)


def next_window(session_name):
    """Switch to the next window in a session."""
    return subprocess.run([TMUX, 'next-window', '-t', session_name])


def split_window(target, split_type, directory, shell=DEFAULT_SHELL):
    """Split a tmux window.
    split_type: '-h' for horizontal split, '-v' for vertical split."""
    return subprocess.run([
        TMUX, 'split-window', split_type, '-t', target,
        '-c', directory, shell
    ], stderr=subprocess.PIPE, stdout=subprocess.PIPE, text=True, check=False)


def select_pane(target):
    """Select a tmux pane."""
    return subprocess.run([TMUX, 'select-pane', '-t', target])


def kill_pane(target):
    """Kill a tmux pane. (CLI use only; deliberately not exposed over MCP.)"""
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
    return cmd  # Return command list for os.execvp()


def new_window_args(*args):
    """Create a window with raw tmux new-window arguments (CLI helper).
    Returns the CompletedProcess."""
    cmd = [TMUX, 'new-window']
    cmd.extend(args)
    return subprocess.run(cmd, stderr=subprocess.PIPE, stdout=subprocess.PIPE, text=True, check=False)


def kill_session(*args):
    """Kill a tmux session. (CLI use only; deliberately not exposed over MCP.)"""
    cmd = [TMUX, 'kill-session']
    cmd.extend(args)
    return subprocess.run(cmd, stderr=subprocess.PIPE, stdout=subprocess.PIPE, text=True, check=False)
