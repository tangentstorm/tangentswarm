"""Run a gate subprocess with a hard timeout, so a hung git fetch cannot hold the serial lock.

A pending-promote run hung for hours on one `git fetch` in hr-gate's scratch repo while it
held the lock, and an hr-gate --sweep hung the same way. Every git and gh call on the gate
path goes through sh():

  - timeout SH_TIMEOUT seconds (env HR_GATE_SH_TIMEOUT, default 120);
  - on a timeout: one line on stderr, then RuntimeError if check, else a result with
    returncode 124 (the code timeout(1) uses), so a caller's normal failure path runs and
    its lock is released by its own finally;
  - on a non-zero exit with check: RuntimeError with the start of stderr.

commit_tree() is the merge-check commit (git commit-tree) with the same timeout.
"""
import os
import subprocess
import sys

SH_TIMEOUT = int(os.environ.get('HR_GATE_SH_TIMEOUT', '120'))
TIMED_OUT = 124


def sh(*a, check=True, timeout=None, env=None, who='hr-gate', run=subprocess.run):
    t = timeout or SH_TIMEOUT
    try:
        r = run(a, capture_output=True, text=True, timeout=t, env=env)
    except subprocess.TimeoutExpired:
        msg = f"{who}: timed out after {t}s: {' '.join(a[:6])}"
        print(msg, file=sys.stderr, flush=True)
        if check:
            raise RuntimeError(msg)
        return subprocess.CompletedProcess(a, TIMED_OUT, '', msg)
    if check and r.returncode:
        raise RuntimeError(f"{' '.join(a[:3])}...: {(r.stderr or '').strip()[:200]}")
    return r


def commit_env(name='hr-gate'):
    env = os.environ.copy()
    env.setdefault('GIT_AUTHOR_NAME', name)
    env.setdefault('GIT_AUTHOR_EMAIL', f'{name}@localhost')
    env.setdefault('GIT_COMMITTER_NAME', name)
    env.setdefault('GIT_COMMITTER_EMAIL', f'{name}@localhost')
    return env


def commit_tree(gitdir, tree, *parents, msg='hr-gate tmp', timeout=None, run=subprocess.run):
    """git commit-tree in a bare repo; the new commit SHA. RuntimeError on a failure or timeout."""
    args = ['git', '--git-dir', gitdir, 'commit-tree', tree]
    for p in parents:
        args += ['-p', p]
    r = sh(*args, '-m', msg, check=False, timeout=timeout, env=commit_env(), run=run)
    if r.returncode:
        raise RuntimeError(f"git commit-tree failed for the merge check (exit {r.returncode})")
    return r.stdout.strip()
