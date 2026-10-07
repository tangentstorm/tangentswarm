"""shell_exec: run a shell command with a timeout, capture output, log the call.

Every call is appended as one JSON line to
~/.local/state/tangentswarm/shell_exec.log (command, cwd, timeout, exit code,
duration, timed_out, output lengths -- never the output itself) and echoed
to stderr. Nothing is ever written to stdout, which belongs to the MCP
stdio protocol.
"""
import asyncio
import json
import os
import signal
import sys
import time
from datetime import datetime, timezone

from . import paths

DEFAULT_TIMEOUT = 60
MAX_TIMEOUT = 600
MAX_OUTPUT_CHARS = 64 * 1024
SHELL_ARGV = ['bash', '-lc']   # login shell: same PATH/env the user gets on ssh


def log_path():
    return paths.state_dir() / 'shell_exec.log'


def clamp_timeout(timeout):
    try:
        t = float(timeout) if timeout is not None else DEFAULT_TIMEOUT
    except (TypeError, ValueError):
        t = DEFAULT_TIMEOUT
    if t <= 0:
        t = DEFAULT_TIMEOUT
    return min(t, MAX_TIMEOUT)


def truncate(text, limit=MAX_OUTPUT_CHARS):
    """Return (text, truncated?) keeping the head and tail of oversized output."""
    if len(text) <= limit:
        return text, False
    head = limit * 3 // 4
    tail = limit - head
    note = (f"\n\n[... output truncated: {len(text)} chars total, showing first {head}"
            f" and last {tail} ...]\n\n")
    return text[:head] + note + text[-tail:], True


def write_log(entry):
    """Append one JSON line to the shell_exec log and mirror it to stderr."""
    line = json.dumps(entry, sort_keys=True)
    try:
        paths.ensure_private_dir(log_path().parent)
        fd = os.open(log_path(), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, 'a') as f:
            f.write(line + '\n')
    except OSError as e:
        print(f"[tangentswarm] could not write shell_exec log: {e}", file=sys.stderr)
    print(f"[tangentswarm] shell_exec {line}", file=sys.stderr, flush=True)


def _kill_group(proc, sig):
    try:
        os.killpg(proc.pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


async def shell_exec(command, cwd=None, timeout=DEFAULT_TIMEOUT, principal=None):
    """Run command via `bash -lc` in cwd. Returns a dict with exit_code,
    stdout, stderr, timed_out, duration_sec and truncation flags."""
    timeout = clamp_timeout(timeout)
    workdir = os.path.expanduser(cwd) if cwd else os.path.expanduser('~')
    started = time.monotonic()
    entry = {
        'ts': datetime.now(timezone.utc).isoformat(timespec='milliseconds'),
        'command': command,
        'cwd': workdir,
        'timeout': timeout,
    }
    if principal:
        entry['principal'] = principal
    timed_out = False
    out_b = err_b = b''
    exit_code = None
    error = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *SHELL_ARGV, command,
            cwd=workdir,
            stdin=asyncio.subprocess.DEVNULL,   # never let a child read MCP stdin
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,             # own process group, so we can kill it all
        )
        try:
            out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            timed_out = True
            _kill_group(proc, signal.SIGTERM)
            try:
                out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=3)
            except asyncio.TimeoutError:
                _kill_group(proc, signal.SIGKILL)
                out_b, err_b = await proc.communicate()
        exit_code = proc.returncode
    except (OSError, ValueError) as e:
        error = f"{type(e).__name__}: {e}"

    duration = round(time.monotonic() - started, 3)
    stdout = out_b.decode('utf-8', errors='replace')
    stderr = err_b.decode('utf-8', errors='replace')
    if error:
        stderr = (stderr + '\n' + error).strip()

    entry.update({
        'exit_code': exit_code,
        'duration_sec': duration,
        'timed_out': timed_out,
        'stdout_len': len(stdout),
        'stderr_len': len(stderr),
    })
    if error:
        entry['error'] = error
    write_log(entry)

    stdout, out_trunc = truncate(stdout)
    stderr, err_trunc = truncate(stderr)
    return {
        'exit_code': exit_code,
        'stdout': stdout,
        'stderr': stderr,
        'timed_out': timed_out,
        'duration_sec': duration,
        'stdout_truncated': out_trunc,
        'stderr_truncated': err_trunc,
        'cwd': workdir,
    }
