"""Pre-trust an agent's worktree before it starts, so no "do you trust this folder?"
screen stalls a launch.

Claude Code (checked against 2.1.302) has no flag or env var that trusts a folder for an
interactive session. It keeps trust in its global config ($CLAUDE_CONFIG_DIR/.claude.json,
else ~/.claude.json) as projects[<path>].hasTrustDialogAccepted. For a linked worktree it
accepts either the main repo's root (the key it writes when you accept the dialog there)
or any directory from the cwd up to the worktree root. It never looks above the git
root, so trusting ~/ver does not cover ~/ver/<repo>.<branch>. We trust the worktree's
real path only, which does not widen trust to the plain checkout.

Claude rewrites that file itself, under a proper-lockfile lock: a directory named
<config>.lock, refreshed every 5s while held and treated as stale after 10s. We take the
same lock and refresh it the same way, re-read, set only our key, write a temp file in the
same directory with the same mode, fsync and os.replace. If the lock was taken over or
replaced meanwhile (its inode or mtime changed) we write nothing and leave it alone.
A missing or unparsable file is never written; the caller gets a warning instead.

Codex (checked against 0.157) keeps trust in $CODEX_HOME/config.toml (default ~/.codex)
as [projects."<path>"] trust_level = "trusted", and also accepts the worktree path.
There is no TOML writer in the stdlib, so a missing table is appended as text, and the
result is parsed again to check that nothing else changed before it is written.

Muse gets --trust-workspace and Grok --trust on their command lines (see registry.py).
Grok (checked against 1.0.62) keeps trust in $GROK_HOME/trusted_folders.toml, keyed by
the main repo root; it ignores an entry for the worktree path, so --trust there trusts
the repo, its plain checkout and every worktree of it. Gemini is not pre-trusted;
agents.blocking_screen() still reports its trust screen.
"""
import json
import os
import stat
import tempfile
import threading
import time
from contextlib import contextmanager

try:
    import tomllib
except ImportError:          # Python 3.10: Codex is then not pre-trusted
    tomllib = None

LOCK_STALE = 10.0       # proper-lockfile's default, which Claude Code uses
LOCK_TIMEOUT = 15.0
LOCK_POLL = 0.05
LOCK_UPDATE = None      # heartbeat interval; None means stale / 2, as in proper-lockfile


class TrustError(RuntimeError):
    """The config could not be updated safely; it was left as it was."""


def claude_config_path():
    base = os.environ.get('CLAUDE_CONFIG_DIR') or os.path.expanduser('~')
    return os.path.join(base, '.claude.json')


def codex_config_path():
    base = os.environ.get('CODEX_HOME') or os.path.join(os.path.expanduser('~'), '.codex')
    return os.path.join(base, 'config.toml')


# ---------------------------------------------------------------------------
# locking and atomic replacement

class _HeldLock:
    """A <config>.lock directory we created. A heartbeat thread touches its mtime every
    `update` seconds so no one takes it over as stale while we hold it. It is ours only
    while its inode and mtime are the ones we last saw: proper-lockfile tells a
    compromised lock the same way, and an owner file inside would make the directory
    nonempty, which Claude Code could then neither take over nor release."""

    def __init__(self, lock, update):
        self.lock = lock
        self.update = update
        self.lost = None
        self._seen = self._stamp()
        self._mu = threading.Lock()
        self._stop = threading.Event()
        self._beat = threading.Thread(target=self._run, name='config-lock-heartbeat', daemon=True)
        self._beat.start()

    def _stamp(self):
        st = os.stat(self.lock)
        return (st.st_ino, st.st_mtime_ns)

    def _verify(self):
        """Set self.lost and return False if the lock is no longer ours. Holds _mu."""
        if self.lost is None:
            try:
                now = self._stamp()
            except OSError as e:
                now = e.strerror
            if now != self._seen:
                self.lost = f"{self.lock} was removed or replaced by another process while held"
        return self.lost is None

    def _run(self):
        while not self._stop.wait(self.update):
            with self._mu:
                if not self._verify():
                    return
                try:
                    os.utime(self.lock)
                    self._seen = self._stamp()
                except OSError as e:
                    self.lost = f"could not refresh {self.lock}: {e.strerror}"
                    return

    def check(self):
        """Raise TrustError unless the lock is still ours (call just before writing)."""
        with self._mu:
            if not self._verify():
                raise TrustError(f"{self.lost}; left as it was")

    def release(self):
        """Stop the heartbeat and remove the lock, but only if it is still ours."""
        self._stop.set()
        self._beat.join()
        with self._mu:
            if self._verify():
                try:
                    os.rmdir(self.lock)
                except OSError:
                    pass


@contextmanager
def config_lock(path, timeout=None, stale=None, sleep=time.sleep, clock=time.time, update=None):
    """Hold <path>.lock the way proper-lockfile does (mkdir, refreshed while held, stale
    after `stale` seconds), so Claude Code and other tangentswarm processes wait for us and
    we wait for them. Yields a _HeldLock; call its check() right before writing."""
    timeout = LOCK_TIMEOUT if timeout is None else timeout
    stale = LOCK_STALE if stale is None else stale
    update = update or LOCK_UPDATE or stale / 2
    lock = os.path.realpath(path) + '.lock'    # beside the target, as proper-lockfile does
    deadline = clock() + timeout
    while True:
        try:
            os.mkdir(lock)
            break
        except FileExistsError:
            try:
                age = clock() - os.stat(lock).st_mtime
            except FileNotFoundError:
                continue
            stuck = ''
            if age > stale:
                try:
                    os.rmdir(lock)      # a holder that died; proper-lockfile does the same
                    continue
                except OSError as e:    # a file or nonempty dir: wait and time out instead
                    stuck = f" (stale but could not be removed: {e.strerror})"
            if clock() >= deadline:
                raise TrustError(f"timed out after {timeout:.0f}s waiting for {lock}{stuck}")
            sleep(LOCK_POLL)
    held = _HeldLock(lock, update)
    try:
        yield held
    finally:
        held.release()


def _fingerprint(st):
    return (st.st_ino, st.st_size, st.st_mtime_ns)


def _replace(real, text, st, held):
    """Write text next to `real` with its mode, fsync, and os.replace it, unless the file
    changed since it was read (st) or the lock `held` is no longer ours."""
    d = os.path.dirname(real)
    fd, tmp = tempfile.mkstemp(dir=d, prefix='.' + os.path.basename(real) + '.', suffix='.tmp')
    try:
        os.fchmod(fd, stat.S_IMODE(st.st_mode))
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        held.check()
        if _fingerprint(os.stat(real)) != _fingerprint(st):
            raise TrustError(f"{real} changed while it was being updated; left as it was")
        os.replace(tmp, real)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    dfd = os.open(d, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def _read(path):
    """Return (realpath, stat, text). Raises FileNotFoundError."""
    real = os.path.realpath(path)
    with open(real, encoding='utf-8') as f:
        st = os.fstat(f.fileno())
        return real, st, f.read()


def _result(agent, path, key, status, **kw):
    return {'agent': agent, 'method': 'config', 'config': path, 'key': key, 'status': status, **kw}


# ---------------------------------------------------------------------------
# Claude Code

def trust_claude(workdir, config=None):
    """Set projects[realpath(workdir)].hasTrustDialogAccepted = true in Claude's global
    config, touching nothing else. Returns a dict whose status is 'trusted',
    'already_trusted' or 'skipped' (with a 'warning'); never raises for a bad config."""
    path = config or claude_config_path()
    key = os.path.realpath(workdir)
    try:
        with config_lock(path) as held:
            try:
                real, st, text = _read(path)
            except FileNotFoundError:
                return _result('claude', path, key, 'skipped', warning=(
                    f"{path} does not exist (Claude Code has never run as this user?); "
                    f"not creating it, so expect first-run screens"))
            try:
                data = json.loads(text)
            except ValueError as e:
                return _result('claude', path, key, 'skipped',
                               warning=f"{path} is not valid JSON ({e}); left untouched")
            projects = data.get('projects', {}) if isinstance(data, dict) else None
            entry = projects.get(key, {}) if isinstance(projects, dict) else None
            if not isinstance(entry, dict):
                return _result('claude', path, key, 'skipped', warning=(
                    f"{path} does not have the expected shape (an object with a 'projects' "
                    f"object); left untouched"))
            if entry.get('hasTrustDialogAccepted') is True:
                return _result('claude', path, key, 'already_trusted')
            data.setdefault('projects', projects)[key] = {**entry, 'hasTrustDialogAccepted': True}
            out = json.dumps(data, indent=2, ensure_ascii=False)
            if text.endswith('\n'):
                out += '\n'
            _replace(real, out, st, held)
    except (TrustError, OSError) as e:
        return _result('claude', path, key, 'skipped', warning=f"could not update {path}: {e}")
    return _result('claude', path, key, 'trusted')


# ---------------------------------------------------------------------------
# Codex

def _toml_string(s):
    if any(ord(c) < 0x20 or c == '\x7f' for c in s):
        raise TrustError(f"path {s!r} has control characters")
    return '"' + s.replace('\\', '\\\\').replace('"', '\\"') + '"'


def trust_codex(workdir, config=None):
    """Add [projects."<realpath(workdir)>"] trust_level = "trusted" to Codex's config.toml.
    An existing entry for the path is never changed, so an explicit "untrusted" stays."""
    path = config or codex_config_path()
    key = os.path.realpath(workdir)
    if tomllib is None:
        return _result('codex', path, key, 'skipped', warning='needs Python 3.11+ (tomllib)')
    try:
        with config_lock(path) as held:
            try:
                real, st, text = _read(path)
            except FileNotFoundError:
                if not os.path.isdir(os.path.dirname(path)):
                    return _result('codex', path, key, 'skipped', warning=(
                        f"{os.path.dirname(path)} does not exist (Codex has never run as this "
                        f"user?); not creating it"))
                real, st, text = None, None, ''
            try:
                data = tomllib.loads(text)
            except tomllib.TOMLDecodeError as e:
                return _result('codex', path, key, 'skipped',
                               warning=f"{path} is not valid TOML ({e}); left untouched")
            projects = data.get('projects', {})
            entry = projects.get(key) if isinstance(projects, dict) else None
            if entry is not None or not isinstance(projects, dict):
                level = entry.get('trust_level') if isinstance(entry, dict) else None
                if level == 'trusted':
                    return _result('codex', path, key, 'already_trusted')
                return _result('codex', path, key, 'skipped', warning=(
                    f"{path} already has a projects entry for {key} "
                    f"(trust_level={level!r}); left as the user set it"))
            sep = '' if not text or text.endswith('\n\n') else ('\n' if text.endswith('\n') else '\n\n')
            out = f'{text}{sep}[projects.{_toml_string(key)}]\ntrust_level = "trusted"\n'
            try:
                new = tomllib.loads(out)
            except tomllib.TOMLDecodeError as e:
                return _result('codex', path, key, 'skipped', warning=(
                    f"appending a [projects] table to {path} would not parse ({e}); "
                    f"left untouched"))
            check = dict(new)
            check['projects'] = {k: v for k, v in check['projects'].items() if k != key}
            if not check['projects'] and 'projects' not in data:
                del check['projects']
            if check != data or new['projects'].get(key) != {'trust_level': 'trusted'}:
                return _result('codex', path, key, 'skipped', warning=(
                    f"appending to {path} would change other settings; left untouched"))
            if real is None:
                held.check()
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, 'w', encoding='utf-8') as f:
                    f.write(out)
                    f.flush()
                    os.fsync(f.fileno())
            else:
                _replace(real, out, st, held)
    except (TrustError, OSError) as e:
        return _result('codex', path, key, 'skipped', warning=f"could not update {path}: {e}")
    return _result('codex', path, key, 'trusted')


# ---------------------------------------------------------------------------

def is_trusted(agent, workdir):
    """Re-read the agent's config: True or False for whether workdir's entry is there, or
    None if that cannot be told (unsupported agent, unreadable config)."""
    key = os.path.realpath(workdir)
    try:
        if agent == 'claude':
            with open(claude_config_path(), encoding='utf-8') as f:
                entry = json.load(f).get('projects', {}).get(key, {})
            return entry.get('hasTrustDialogAccepted') is True
        if agent == 'codex' and tomllib is not None:
            with open(codex_config_path(), 'rb') as f:
                entry = tomllib.load(f).get('projects', {}).get(key, {})
            return entry.get('trust_level') == 'trusted'
    except (OSError, ValueError, AttributeError):
        return None
    return None


TRUST_FLAGS = {'muse': '--trust-workspace', 'grok': '--trust'}    # must match registry flags


def _flag_result(agent, status):
    out = {'agent': agent, 'method': 'flag', 'flag': TRUST_FLAGS[agent], 'status': status}
    if agent == 'grok':
        out['scope'] = 'repo'      # Grok trusts the main repo root, not just the worktree
    return out


def trust_state(agent, workdir):
    """Report workdir's current trust for `agent` without writing anything (used when
    start_agent reuses a running agent)."""
    if agent in TRUST_FLAGS:
        return _flag_result(agent, 'reused')
    if agent not in ('claude', 'codex'):
        return {'agent': agent, 'method': None, 'status': 'unsupported'}
    config = claude_config_path() if agent == 'claude' else codex_config_path()
    return _result(agent, config, os.path.realpath(workdir), 'reused',
                   trusted=is_trusted(agent, workdir))


def pretrust(agent, workdir):
    """Trust workdir for `agent` before it starts. Never raises: a config that cannot be
    updated safely is reported in the result's 'warning' and the launch goes ahead."""
    if agent == 'claude':
        return trust_claude(workdir)
    if agent == 'codex':
        return trust_codex(workdir)
    if agent in TRUST_FLAGS:
        return _flag_result(agent, 'trusted')
    return {'agent': agent, 'method': None, 'status': 'unsupported'}
