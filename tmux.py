#!/usr/bin/env python3
import subprocess

def has_session(session_name):
    """Check if a tmux session exists."""
    try:
        result = subprocess.run(['tmux', 'has-session', '-t', session_name],
                              stderr=subprocess.PIPE, check=False)
        return result.returncode == 0
    except Exception:
        return False

def new_session(session_name, directory, shell="${SHELL:-bash}"):
    """Create a new tmux session."""
    return subprocess.run([
        'tmux', 'new-session', '-d', '-s', session_name,
        '-c', directory, shell
    ], stderr=subprocess.PIPE, stdout=subprocess.PIPE, text=True, check=False)

def rename_window(target, name):
    """Rename a tmux window."""
    return subprocess.run(['tmux', 'rename-window', '-t', target, name])

def send_keys(target, keys, enter=True):
    """Send keys to a tmux pane.
    If enter=True, adds an 'Enter' key press at the end."""
    cmd = ['tmux', 'send-keys', '-t', target, keys]
    if enter:
        cmd.append('Enter')
    return subprocess.run(cmd)

def run_command(command):
    """Run a command directly without a shell."""
    return subprocess.run(command.split(), stderr=subprocess.PIPE, stdout=subprocess.PIPE, text=True, check=False)

def run_tmux_command(session_name, command):
    """Run a tmux command against the specified session."""
    # Prepend the session specification to the command
    cmd = ['tmux'] + command.split()
    # Add the target session if it's not already specified
    if '-t' not in cmd:
        cmd.extend(['-t', session_name])
    return subprocess.run(cmd, stderr=subprocess.PIPE, stdout=subprocess.PIPE, text=True, check=False)

def next_window(session_name):
    """Switch to the next window in a session."""
    return subprocess.run(['tmux', 'next-window', '-t', session_name])

def list_panes(session_name, format_str=None):
    """List panes in a tmux session with optional format string."""
    cmd = ['tmux', 'list-panes', '-t', session_name]
    if format_str:
        cmd.extend(['-F', format_str])
    return subprocess.run(cmd, stderr=subprocess.PIPE,
                         stdout=subprocess.PIPE, text=True, check=False)

def split_window(target, split_type, directory, shell="${SHELL:-bash}"):
    """Split a tmux window.
    split_type: '-h' for horizontal split, '-v' for vertical split."""
    return subprocess.run([
        'tmux', 'split-window', split_type, '-t', target,
        '-c', directory, shell
    ], stderr=subprocess.PIPE, stdout=subprocess.PIPE, text=True, check=False)

def select_pane(target):
    """Select a tmux pane."""
    return subprocess.run(['tmux', 'select-pane', '-t', target])

def kill_pane(target):
    """Kill a tmux pane."""
    return subprocess.run(['tmux', 'kill-pane', '-t', target], check=False)

def switch_client(target):
    """Switch tmux client to the specified session."""
    return subprocess.run(['tmux', 'switch-client', '-t', target])

def attach_session(session_name, unicode=True):
    """Attach to a tmux session."""
    cmd = ['tmux']
    if unicode:
        cmd.append('-u')
    cmd.extend(['attach-session', '-t', session_name])
    return cmd  # Return command list for os.execvp()

def new_window(*args):
    """Create a new window in a tmux session."""
    cmd = ['tmux', 'new-window']
    cmd.extend(args)
    return subprocess.run(cmd, stderr=subprocess.PIPE, stdout=subprocess.PIPE, text=True, check=False)

def kill_session(*args):
    """Kill a tmux session."""
    cmd = ['tmux', 'kill-session']
    cmd.extend(args)
    return subprocess.run(cmd, stderr=subprocess.PIPE, stdout=subprocess.PIPE, text=True, check=False)