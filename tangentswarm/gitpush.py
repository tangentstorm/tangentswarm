"""Push to the platform origin from hr-gate's scratch repo, never from the caller's cwd.

pending-promote once ran `git push` in whatever directory its caller was in (the
swarm-promoted daemon runs in a tangentswarm checkout; other callers run elsewhere). That
repo did not hold the platform commit, so a force-with-lease push of a -vN tip failed
even though both SHAs matched GitHub, and the error hid git's stderr.

push() runs in the scratch repo (it borrows the platform checkout's objects and fetches a
missing commit from origin first) and raises PushError with git's own stderr.
"""


class PushError(RuntimeError):
    pass


def push(scratch, url, refspecs, lease=None, need=(), dry_run=False, what='push'):
    """git push url refspecs... from the scratch repo.

    scratch   an hr-gate ScratchRepo (open): .ensure(*shas) and .git(*args, check=False)
    lease     (ref, expected sha) for --force-with-lease=ref:sha, or None
    need      commits the push sends; fetched into the scratch repo first if missing
    """
    need = [s for s in need if s]
    if need:
        scratch.ensure(*need)
    args = ['push', '--porcelain']
    if dry_run:
        args.append('--dry-run')
    if lease:
        ref, sha = lease
        args.append(f'--force-with-lease={ref}:{sha}')
    r = scratch.git(*args, url, *refspecs, check=False)
    if r.returncode:
        err = ' '.join(((r.stderr or '') + ' ' + (r.stdout or '')).split())
        raise PushError(f"{what} failed (git exit {r.returncode}): {err[:400]}")
    return r
