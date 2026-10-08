#!/usr/bin/env python3
import subprocess

def run_git_cmd(args, cwd=None, capture_output=True, text=True, check=False, stdout=None, stderr=None):
    """Common helper function to run git commands with consistent defaults."""
    cmd = ['git'] + args
    return subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=capture_output,
        text=text,
        check=check,
        stdout=stdout,
        stderr=stderr
    )

def clone(repo_url, target_dir, check=True):
    """Clone a git repository."""
    return run_git_cmd(['clone', repo_url, target_dir], check=check)

def checkout(branch_name, cwd=None, check=False):
    """Checkout an existing branch."""
    return run_git_cmd(['checkout', branch_name], cwd=cwd, check=check)

def checkout_new_branch(branch_name, cwd=None, check=False):
    """Create and checkout a new branch."""
    return run_git_cmd(['checkout', '-b', branch_name], cwd=cwd, check=check)

def checkout_track_branch(branch_name, remote_branch, cwd=None, check=False):
    """Create and checkout a new branch tracking a remote branch."""
    return run_git_cmd(['checkout', '-b', branch_name, '--track', remote_branch], cwd=cwd, check=check)

def branch_list(cwd=None, check=True):
    """List all local branches."""
    return run_git_cmd(['branch'], cwd=cwd, check=check)

def branch_show_current(cwd=None, check=True):
    """Show the current branch name."""
    return run_git_cmd(['branch', '--show-current'], cwd=cwd, check=check)

def branch_set_upstream(branch_name, remote_branch, cwd=None, check=False):
    """Set the upstream branch for the current branch."""
    return run_git_cmd(['branch', '--set-upstream-to', remote_branch, branch_name], cwd=cwd, check=check)

def branch_verbose(cwd=None, check=True):
    """List branches with verbose tracking info."""
    return run_git_cmd(['branch', '-vv'], cwd=cwd, check=check)

def remote_branches(remote='origin', branch_pattern=None, cwd=None, check=True):
    """List remote branches, optionally filtered by pattern."""
    args = ['ls-remote', '--heads', remote]
    if branch_pattern:
        args.append(branch_pattern)
    return run_git_cmd(args, cwd=cwd, check=check)

def pull(cwd=None, ff_only=True, check=False):
    """Pull changes from remote."""
    args = ['pull']
    if ff_only:
        args.append('--ff-only')
    return run_git_cmd(args, cwd=cwd, check=check)

def push(remote='origin', branch=None, set_upstream=False, cwd=None, check=False):
    """Push changes to remote."""
    args = ['push']
    if set_upstream:
        args.append('-u')
    args.append(remote)
    if branch:
        args.append(branch)
    return run_git_cmd(args, cwd=cwd, check=check)

def config_set(key, value, cwd=None, check=False):
    """Set a git config value."""
    return run_git_cmd(['config', key, value], cwd=cwd, check=check)

def config_get(key, cwd=None, check=False):
    """Get a git config value."""
    return run_git_cmd(['config', '--get', key], cwd=cwd, check=check)

def status(cwd=None, check=False):
    """Get git repository status."""
    return run_git_cmd(['status'], cwd=cwd, check=check)

def diff(cwd=None, check=False):
    """Show git diff."""
    return run_git_cmd(['diff'], cwd=cwd, check=check)

def log(count=None, format=None, cwd=None, check=False):
    """Show git log."""
    args = ['log']
    if count:
        args.extend(['-n', str(count)])
    if format:
        args.extend(['--format', format])
    return run_git_cmd(args, cwd=cwd, check=check)

def add(files, cwd=None, check=False):
    """Add files to git staging area."""
    if isinstance(files, str):
        files = [files]
    args = ['add']
    args.extend(files)
    return run_git_cmd(args, cwd=cwd, check=check)

def commit(message, cwd=None, check=False):
    """Create a git commit."""
    return run_git_cmd(['commit', '-m', message], cwd=cwd, check=check)

def fetch(remote='origin', branch=None, cwd=None, check=False):
    """Fetch changes from remote."""
    args = ['fetch', remote]
    if branch:
        args.append(branch)
    return run_git_cmd(args, cwd=cwd, check=check)

def reset(target, mode=None, cwd=None, check=False):
    """Reset current HEAD to specified state."""
    args = ['reset']
    if mode:
        args.append(mode)
    args.append(target)
    return run_git_cmd(args, cwd=cwd, check=check)

def stash_save(message=None, cwd=None, check=False):
    """Save changes to the stash."""
    args = ['stash', 'save']
    if message:
        args.append(message)
    return run_git_cmd(args, cwd=cwd, check=check)

def stash_pop(cwd=None, check=False):
    """Apply and remove changes from the stash."""
    return run_git_cmd(['stash', 'pop'], cwd=cwd, check=check)

def stash_list(cwd=None, check=False):
    """List stashed changes."""
    return run_git_cmd(['stash', 'list'], cwd=cwd, check=check)

def set_upstream_tracking(branch, remote='origin', cwd=None):
    """Configure branch's upstream tracking without pushing.
    
    This sets the tracking configuration directly in git config.
    """
    config_set(f'branch.{branch}.remote', remote, cwd=cwd)
    config_set(f'branch.{branch}.merge', f'refs/heads/{branch}', cwd=cwd)