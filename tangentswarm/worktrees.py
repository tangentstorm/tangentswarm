"""One git worktree per agent, shared by the swarm CLI and swarm-mcp.

Layout, under the top directory (the agent root, ~/ver by default):

  <top>/<repo>            the plain checkout. It always stays on the default branch
                          (origin/HEAD, e.g. main) and is fast-forwarded on every spawn.
  <top>/<repo>.<slug>     one linked worktree per branch, as a sibling of the repo.

The slug is the branch name with every '/' replaced by '-', so `minavo/auth-fix` lives
at <top>/platform.minavo-auth-fix. Branch names may only use [A-Za-z0-9._/-], so the slug
is always a single safe path component. Two branch names can map to the same slug
(`a/b` and `a-b`). When the directory already holds the other branch, the spawn is
refused rather than reusing it.

Every spawn:
  1. fetches origin and fast-forwards <repo> to origin/<default>. It refuses loudly if
     <repo> is not on the default branch, is dirty (untracked files count) or has
     diverged from origin;
  2. creates <repo>.<slug> if needed: from the local branch if it exists, else from
     origin/<branch> if that exists, else as a new branch cut from the up-to-date
     default branch. Retrying is safe: an existing worktree for the same branch is
     reused, and a live agent of the same kind already in it is returned instead of
     a second one being started;
  3. applies the repo's .swarm.yaml (see load_config) and launches the agent there.

Worktree operations on one repo are serialised with a lock file in its git dir.
Nothing here runs a command from the repo or the caller: only git and tmux.
"""
import argparse
import fcntl
import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
from contextlib import contextmanager

import yaml

from . import registry, tmux

CONFIG_FILE = '.swarm.yaml'
CONFIG_KEYS = ('symlink', 'copy')
EXCLUDE_BEGIN = '# >>> tangentswarm worktree links (managed) >>>'
EXCLUDE_END = '# <<< tangentswarm worktree links <<<'

BRANCH_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$')
CONFIG_PATH_RE = re.compile(r'^[A-Za-z0-9._@+-]+(?:/[A-Za-z0-9._@+-]+)*$')
GIT_TIMEOUT = 120


class WorktreeError(registry.AgentError):
    """A worktree request that is refused, with a message meant for a person."""


# ---------------------------------------------------------------------------
# git

def _git_env():
    env = dict(os.environ, GIT_TERMINAL_PROMPT='0')
    env.setdefault('GIT_SSH_COMMAND', 'ssh -o BatchMode=yes')
    return env


def git(args, cwd, check=True, timeout=GIT_TIMEOUT):
    try:
        r = subprocess.run(['git', *args], cwd=cwd, stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, env=_git_env(), timeout=timeout)
    except subprocess.TimeoutExpired:
        raise WorktreeError(f"git {' '.join(args)} timed out after {timeout}s in {cwd}")
    if check and r.returncode != 0:
        raise WorktreeError(f"git {' '.join(args)} failed in {cwd}: "
                            f"{(r.stderr or r.stdout).strip()}")
    return r


def _ok(args, cwd):
    return git(args, cwd, check=False).returncode == 0


def _out(args, cwd):
    return git(args, cwd).stdout.strip()


# ---------------------------------------------------------------------------
# names and paths

def top_dir(top=None):
    return os.path.realpath(os.path.expanduser(top)) if top else registry.agent_root()


def validate_branch(branch, cwd=None):
    if not isinstance(branch, str) or not BRANCH_RE.match(branch) or '..' in branch \
            or '//' in branch or branch.endswith(('/', '.', '.lock')) or '/.' in branch:
        raise WorktreeError(f"branch {branch!r} must be 1-100 chars of [A-Za-z0-9._/-], "
                            f"start with a letter or digit, and be a valid git branch name")
    if cwd and not _ok(['check-ref-format', '--branch', branch], cwd):
        raise WorktreeError(f"branch {branch!r} is not a valid git branch name")
    return branch


def branch_slug(branch):
    """The directory suffix for a branch: every '/' becomes '-'."""
    return validate_branch(branch).replace('/', '-')


def worktree_path(top, repo_name, branch):
    return os.path.join(top_dir(top), f"{repo_name}.{branch_slug(branch)}")


def _inside(path, root):
    return os.path.commonpath([root, path]) == root


def plain_repo(repo, top=None):
    """Resolve `repo` (a name under top, or a path to it) to the plain checkout, which must
    be a directory directly under top and a git main worktree (not a linked one)."""
    top = top_dir(top)
    if not isinstance(repo, str) or not repo.strip() or any(c in repo for c in '\0\n\r'):
        raise WorktreeError(f"repo must name a git checkout directly under {top}")
    p = os.path.expanduser(repo.strip())
    real = os.path.realpath(p if os.path.isabs(p) else os.path.join(top, p))
    if os.path.dirname(real) != top:
        raise WorktreeError(f"repo {repo!r} must be a directory directly under {top} "
                            f"(resolved to {real})")
    if not os.path.isdir(real):
        raise WorktreeError(f"repo {repo!r} is not an existing directory under {top}")
    r = git(['rev-parse', '--show-toplevel', '--absolute-git-dir', '--git-common-dir'],
            real, check=False)
    if r.returncode != 0:
        raise WorktreeError(f"{real} is not a git repository")
    toplevel, git_dir, common = r.stdout.splitlines()[:3]
    if os.path.realpath(toplevel) != real:
        raise WorktreeError(f"{real} is inside the repo {toplevel}, not its top level")
    common = os.path.realpath(os.path.join(real, common))
    if os.path.realpath(git_dir) != common:
        raise WorktreeError(f"{real} is a linked worktree of {os.path.dirname(common)}; "
                            f"pass the plain repo instead")
    return real


@contextmanager
def repo_lock(repo_path):
    common = os.path.realpath(os.path.join(repo_path, _out(['rev-parse', '--git-common-dir'],
                                                           repo_path)))
    with open(os.path.join(common, 'tangentswarm-worktree.lock'), 'w') as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


# ---------------------------------------------------------------------------
# keeping the plain checkout on an up-to-date default branch

def default_branch(repo_path, network=True):
    """The branch origin/HEAD points at ('main'). With network, ask origin if unset."""
    if not _ok(['remote', 'get-url', 'origin'], repo_path):
        raise WorktreeError(f"{repo_path} has no 'origin' remote")
    r = git(['symbolic-ref', '--short', 'refs/remotes/origin/HEAD'], repo_path, check=False)
    if r.returncode != 0 and network:
        git(['remote', 'set-head', 'origin', '--auto'], repo_path)
        r = git(['symbolic-ref', '--short', 'refs/remotes/origin/HEAD'], repo_path, check=False)
    if r.returncode != 0:
        if not network:
            return None
        raise WorktreeError(f"cannot tell the default branch of {repo_path}: origin/HEAD is unset")
    return r.stdout.strip().split('/', 1)[1]


def _dirty_lines(path):
    # --no-optional-locks: never take index.lock in a worktree an agent may be using
    return git(['--no-optional-locks', 'status', '--porcelain', '--untracked-files=normal'],
               path).stdout.splitlines()


def sync_default(repo_path):
    """Fetch origin and fast-forward the plain checkout to origin/<default>.
    Returns (default branch, commit sha). Refuses if it cannot fast-forward cleanly."""
    git(['fetch', '--prune', 'origin'], repo_path)
    default = default_branch(repo_path)
    current = _out(['branch', '--show-current'], repo_path)
    if current != default:
        raise WorktreeError(
            f"REFUSING: {repo_path} is on {current or 'a detached HEAD'}, but the plain "
            f"checkout must stay on {default}. Move that work into a worktree "
            f"({os.path.basename(repo_path)}.<branch>) and `git switch {default}` there.")
    dirty = _dirty_lines(repo_path)
    if dirty:
        shown = '\n  '.join(dirty[:10]) + ('\n  ...' if len(dirty) > 10 else '')
        raise WorktreeError(f"REFUSING: {repo_path} has uncommitted changes, so it cannot be "
                            f"fast-forwarded to origin/{default}:\n  {shown}")
    local = _out(['rev-parse', 'HEAD'], repo_path)
    remote = _out(['rev-parse', f'refs/remotes/origin/{default}'], repo_path)
    if local != remote:
        if not _ok(['merge-base', '--is-ancestor', local, remote], repo_path):
            ahead = _out(['rev-list', '--count', f'{remote}..{local}'], repo_path)
            behind = _out(['rev-list', '--count', f'{local}..{remote}'], repo_path)
            raise WorktreeError(
                f"REFUSING: {default} in {repo_path} has diverged from origin/{default} "
                f"({ahead} commits ahead, {behind} behind). Push or move those commits to a "
                f"branch, then reset {default} to origin/{default}.")
        git(['merge', '--ff-only', '--quiet', remote], repo_path)
    return default, remote


# ---------------------------------------------------------------------------
# worktree listing

def worktree_entries(repo_path):
    """Parse `git worktree list --porcelain` into dicts: path, head, branch, detached,
    prunable, main (the first entry is the main worktree)."""
    entries, cur = [], None
    for line in git(['worktree', 'list', '--porcelain'], repo_path).stdout.splitlines() + ['']:
        if not line:
            if cur:
                entries.append(cur)
            cur = None
            continue
        key, _, val = line.partition(' ')
        if key == 'worktree':
            cur = {'path': os.path.realpath(val), 'head': None, 'branch': None,
                   'detached': False, 'prunable': False, 'main': not entries}
        elif cur is None:
            continue
        elif key == 'HEAD':
            cur['head'] = val
        elif key == 'branch':
            cur['branch'] = val.removeprefix('refs/heads/')
        elif key == 'detached':
            cur['detached'] = True
        elif key == 'prunable':
            cur['prunable'] = True
    return entries


def _all_panes():
    try:
        return tmux.list_panes(all=True)
    except (tmux.TmuxError, OSError):
        return []


def panes_in(path, panes=None):
    """tmux panes whose current path is the worktree or under it."""
    return [p for p in (_all_panes() if panes is None else panes)
            if p.get('current_path') and _inside(os.path.realpath(p['current_path']), path)]


def processes_in(path):
    """[(pid, comm)] of this user's processes whose cwd is inside path (Linux /proc)."""
    out, me = [], os.getpid()
    try:
        pids = [int(d) for d in os.listdir('/proc') if d.isdigit()]
    except OSError:
        return out
    for pid in pids:
        if pid == me:
            continue
        try:
            cwd = os.readlink(f'/proc/{pid}/cwd')
            with open(f'/proc/{pid}/comm', encoding='utf-8', errors='replace') as f:
                comm = f.read().strip()
        except OSError:
            continue
        if cwd.endswith(' (deleted)'):
            continue
        if _inside(cwd, path):
            out.append((pid, comm))
    return out


def _pane_summary(p):
    return {'pane_id': p['pane_id'], 'target': f"{p['session']}:{p['window_index']}",
            'window_name': p['window_name'], 'command': p['current_command']}


def list_worktrees(repo=None, top=None):
    """Every git repo directly under top (or just `repo`), with its worktrees: path,
    branch, head, whether it follows the <repo>.<slug> layout, dirty, and the tmux
    panes working in it. Read-only: no fetch."""
    top = top_dir(top)
    if repo is not None:
        repos = [plain_repo(repo, top)]
    else:
        repos = []
        for name in sorted(os.listdir(top)):
            p = os.path.join(top, name)
            if os.path.isdir(p) and not os.path.islink(p) and os.path.exists(os.path.join(p, '.git')):
                try:
                    repos.append(plain_repo(p, top))
                except WorktreeError:
                    pass    # linked worktrees are listed under their repo
    panes = _all_panes()
    out = []
    for rp in repos:
        name = os.path.basename(rp)
        try:
            default = default_branch(rp, network=False)
        except WorktreeError:
            default = None
        wts = []
        for e in worktree_entries(rp):
            if e['main']:
                layout = e['branch'] == default if default else None
            else:
                layout = bool(e['branch']) and BRANCH_RE.match(e['branch']) is not None and \
                    e['path'] == os.path.join(top, f"{name}.{e['branch'].replace('/', '-')}")
            dirty = None
            if not e['prunable'] and os.path.isdir(e['path']):
                dirty = bool(_dirty_lines(e['path']))
            wts.append({**e, 'layout': layout, 'dirty': dirty,
                        'panes': [_pane_summary(p) for p in panes_in(e['path'], panes)]})
        out.append({'repo': name, 'path': rp, 'default_branch': default, 'worktrees': wts})
    return {'top': top, 'repos': out}


# ---------------------------------------------------------------------------
# .swarm.yaml: files and dirs to share with each worktree

def load_config(repo_path):
    """Read <repo>/.swarm.yaml, committed on the default branch:

        worktree:
          symlink:            # linked from the plain checkout into each worktree
            - node_modules
            - .lake/packages
          copy:               # copied once into each worktree (never overwritten)
            - .claude/settings.local.json

    Paths are relative to the repo root, use only [A-Za-z0-9._@+-] and '/', and may not
    contain '..' or touch .git. Nothing in this file is ever run."""
    p = os.path.join(repo_path, CONFIG_FILE)
    if not os.path.isfile(p):
        return {k: [] for k in CONFIG_KEYS}
    try:
        with open(p, encoding='utf-8') as f:
            data = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError) as e:
        raise WorktreeError(f"{p}: {e}")
    wt = data.get('worktree') if isinstance(data, dict) else None
    if wt is None:
        return {k: [] for k in CONFIG_KEYS}
    if not isinstance(wt, dict) or set(wt) - set(CONFIG_KEYS):
        raise WorktreeError(f"{p}: 'worktree' must be a mapping with only {', '.join(CONFIG_KEYS)}")
    out = {}
    for key in CONFIG_KEYS:
        items = wt.get(key) or []
        if not isinstance(items, list):
            raise WorktreeError(f"{p}: worktree.{key} must be a list of paths")
        out[key] = [_config_path(p, key, i) for i in items]
    return out


def _config_path(cfg, key, item):
    if not isinstance(item, str) or not CONFIG_PATH_RE.match(item) \
            or posixpath.normpath(item) != item \
            or any(part in ('.', '..', '.git') for part in item.split('/')):
        raise WorktreeError(f"{cfg}: worktree.{key} entry {item!r} must be a plain relative "
                            f"path inside the repo (no '..', no .git, no glob characters)")
    return item


def _update_exclude(repo_path, rels):
    """Keep the shared info/exclude block listing every linked or copied path, so links like
    node_modules (which a `node_modules/` gitignore rule does not match) never make a
    worktree look dirty."""
    common = os.path.realpath(os.path.join(repo_path, _out(['rev-parse', '--git-common-dir'],
                                                           repo_path)))
    path = os.path.join(common, 'info', 'exclude')
    try:
        with open(path, encoding='utf-8') as f:
            text = f.read()
    except FileNotFoundError:
        text = ''
    lines = text.splitlines()
    if EXCLUDE_BEGIN in lines and EXCLUDE_END in lines:
        a, b = lines.index(EXCLUDE_BEGIN), lines.index(EXCLUDE_END)
        old = lines[a + 1:b]
        lines = lines[:a] + lines[b + 1:]
    else:
        old = []
    entries = sorted(set(old) | {'/' + r for r in rels})
    if not entries:
        return
    new = lines + [EXCLUDE_BEGIN, *entries, EXCLUDE_END]
    if new != text.splitlines():
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(new) + '\n')


def apply_config(repo_path, wt_path, config=None):
    """Symlink and copy the configured paths from the plain checkout into the worktree.
    Idempotent: existing links to the right place are kept, and anything else already at
    a destination is left alone and reported. Returns {'linked', 'copied', 'skipped'}."""
    config = config if config is not None else load_config(repo_path)
    result = {'linked': [], 'copied': [], 'skipped': []}
    _update_exclude(repo_path, config['symlink'] + config['copy'])
    for key in CONFIG_KEYS:
        for rel in config[key]:
            src = os.path.join(repo_path, rel)
            real_src = os.path.realpath(src)
            if not os.path.lexists(src):
                result['skipped'].append({'path': rel, 'reason': f'missing in {repo_path}'})
                continue
            if not _inside(real_src, repo_path) or _inside(real_src, os.path.join(repo_path, '.git')):
                result['skipped'].append({'path': rel, 'reason': 'resolves outside the repo'})
                continue
            dest = os.path.join(wt_path, rel)
            parent = os.path.dirname(dest)
            if not _inside(os.path.realpath(parent), wt_path):
                result['skipped'].append({'path': rel, 'reason': 'destination leaves the worktree'})
                continue
            if os.path.lexists(dest):
                if key == 'symlink' and os.path.islink(dest) and os.readlink(dest) == real_src:
                    result['linked'].append(rel)
                elif key == 'copy':
                    result['copied'].append(rel)
                else:
                    result['skipped'].append({'path': rel, 'reason': 'already exists in the worktree'})
                continue
            os.makedirs(parent, exist_ok=True)
            if not _inside(os.path.realpath(parent), wt_path):
                result['skipped'].append({'path': rel, 'reason': 'destination leaves the worktree'})
                continue
            if key == 'symlink':
                os.symlink(real_src, dest)
                result['linked'].append(rel)
            elif os.path.isdir(real_src):
                shutil.copytree(real_src, dest, symlinks=True)
                result['copied'].append(rel)
            else:
                shutil.copy2(real_src, dest)
                result['copied'].append(rel)
    return result


# ---------------------------------------------------------------------------
# creating, spawning and removing

def _ensure_locked(repo_path, branch, top):
    name = os.path.basename(repo_path)
    default, base = sync_default(repo_path)
    validate_branch(branch, repo_path)
    if branch == default:
        raise WorktreeError(f"{name} itself tracks {default}; give the agent its own branch")
    path = worktree_path(top, name, branch)
    git(['worktree', 'prune'], repo_path)
    entries = worktree_entries(repo_path)
    at_path = next((e for e in entries if e['path'] == path), None)
    on_branch = next((e for e in entries if e['branch'] == branch), None)
    if at_path:
        if at_path['branch'] != branch:
            raise WorktreeError(
                f"{path} already holds {at_path['branch'] or 'a detached HEAD'}; branch "
                f"{branch!r} maps to the same directory ('/' becomes '-'). Pick another name.")
        source = 'existing'
    elif on_branch:
        raise WorktreeError(f"branch {branch!r} is already checked out at {on_branch['path']}")
    elif os.path.lexists(path):
        raise WorktreeError(f"{path} exists but is not a worktree of {repo_path}")
    elif _ok(['show-ref', '--verify', '--quiet', f'refs/heads/{branch}'], repo_path):
        git(['worktree', 'add', '--quiet', path, branch], repo_path)
        source = 'local'
    elif _ok(['show-ref', '--verify', '--quiet', f'refs/remotes/origin/{branch}'], repo_path):
        git(['worktree', 'add', '--quiet', '--track', '-b', branch, path, f'origin/{branch}'],
            repo_path)
        source = 'origin'
    else:
        git(['worktree', 'add', '--quiet', '--no-track', '-b', branch, path, base], repo_path)
        source = 'new'
    real = os.path.realpath(path)
    if real != path or not _inside(real, top):
        raise WorktreeError(f"worktree {path} resolved to {real}, outside {top}")
    links = apply_config(repo_path, path)
    return {'repo': name, 'repo_path': repo_path, 'branch': branch, 'path': path,
            'default_branch': default, 'base': base, 'source': source,
            'head': _out(['rev-parse', 'HEAD'], path), **links}


def ensure_worktree(repo, branch, top=None):
    """Sync the plain checkout, then create or reuse <top>/<repo>.<slug> for branch."""
    top = top_dir(top)
    repo_path = plain_repo(repo, top)
    validate_branch(branch)
    with repo_lock(repo_path):
        return _ensure_locked(repo_path, branch, top)


def _default_window(agent, branch):
    slug = re.sub(r'[^a-z0-9]+', '-', branch.lower()).strip('-')
    return f"{agent}-{slug}"[:40].strip('-')


def _live_agents(path):
    from . import agents
    found = []
    for p in panes_in(path):
        try:
            info = agents.pane_agent(p['pane_id'])
        except (agents.NotAnAgentPane, tmux.TmuxError):
            continue
        if info['agent']:
            found.append({**_pane_summary(p), 'agent': info['agent']})
    return found


def spawn_agent(agent, repo, branch, session='agents', window_name=None, top=None, run=None):
    """Start a registered agent in its own worktree of repo on branch (see module doc).
    The window name defaults to '<agent>-<branch slug>'."""
    argv = registry.launch_argv(agent)
    registry.validate_session(session)
    name = registry.validate_window_name(window_name) if window_name \
        else _default_window(agent, validate_branch(branch))
    top = top_dir(top)
    repo_path = plain_repo(repo, top)
    validate_branch(branch)
    with repo_lock(repo_path):
        wt = _ensure_locked(repo_path, branch, top)
        live = _live_agents(wt['path'])
        same = [a for a in live if a['agent'] == agent]
        if same:
            return {'agent': agent, 'argv': argv, 'cwd': wt['path'], 'session': same[0]['target'].split(':')[0],
                    'window_name': same[0]['window_name'], 'pane': same[0], 'reused': True,
                    'worktree': wt}
        if live:
            raise WorktreeError(f"{live[0]['agent']} is already working in {wt['path']} "
                                f"({live[0]['pane_id']}); one agent per worktree")
        out = registry.launch_agent(agent, wt['path'], session=session, window_name=name, run=run)
    return {**out, 'reused': False, 'worktree': wt}


def remove_worktree(repo, branch, delete_branch=False, top=None):
    """Remove <repo>.<slug> for branch. Refuses if any tmux pane or process is in it, or if
    it has uncommitted or untracked changes. With delete_branch, the branch is deleted
    only if it is merged into origin/<default>; otherwise it is kept and reported."""
    top = top_dir(top)
    repo_path = plain_repo(repo, top)
    validate_branch(branch)
    with repo_lock(repo_path):
        git(['worktree', 'prune'], repo_path)
        entry = next((e for e in worktree_entries(repo_path) if e['branch'] == branch), None)
        if entry is None:
            raise WorktreeError(f"no worktree of {repo_path} has {branch!r} checked out")
        path = entry['path']
        if entry['main']:
            raise WorktreeError(f"{path} is the plain checkout; it is never removed")
        panes = panes_in(path)
        procs = processes_in(path)
        if panes or procs:
            who = [f"{p['pane_id']} ({p['window_name']}: {p['current_command']})" for p in panes] \
                + [f"pid {pid} ({comm})" for pid, comm in procs]
            raise WorktreeError(f"REFUSING: {path} is in use by " + ', '.join(who))
        dirty = _dirty_lines(path)
        if dirty:
            shown = '\n  '.join(dirty[:10]) + ('\n  ...' if len(dirty) > 10 else '')
            raise WorktreeError(f"REFUSING: {path} has uncommitted or untracked changes:\n  {shown}")
        head = entry['head']
        git(['worktree', 'remove', path], repo_path)
        git(['worktree', 'prune'], repo_path)
        out = {'repo': os.path.basename(repo_path), 'branch': branch, 'path': path,
               'head': head, 'removed': True, 'branch_deleted': False}
        if delete_branch:
            git(['fetch', '--prune', 'origin'], repo_path, check=False)
            default = default_branch(repo_path)
            if _ok(['merge-base', '--is-ancestor', f'refs/heads/{branch}',
                    f'refs/remotes/origin/{default}'], repo_path):
                git(['branch', '-D', branch], repo_path)
                out['branch_deleted'] = True
            else:
                out['branch_kept'] = (f"{branch} is not merged into origin/{default} (a squash "
                                      f"merge looks unmerged); delete it with git branch -D")
        return out


# ---------------------------------------------------------------------------
# CLI: swarm -c start-agent | worktrees | remove-worktree

def _print(obj):
    print(json.dumps(obj, indent=2))


def cli(name, argv):
    try:
        if name == 'start-agent':
            ap = argparse.ArgumentParser(prog='swarm -c start-agent')
            ap.add_argument('agent', choices=registry.AGENT_NAMES)
            ap.add_argument('repo')
            ap.add_argument('branch')
            ap.add_argument('--session', default='agents')
            ap.add_argument('--window')
            a = ap.parse_args(argv)
            _print(spawn_agent(a.agent, a.repo, a.branch, session=a.session, window_name=a.window))
        elif name == 'worktrees':
            ap = argparse.ArgumentParser(prog='swarm -c worktrees')
            ap.add_argument('repo', nargs='?')
            ap.add_argument('--json', action='store_true')
            a = ap.parse_args(argv)
            data = list_worktrees(a.repo)
            if a.json:
                _print(data)
            else:
                _print_table(data)
        elif name == 'remove-worktree':
            ap = argparse.ArgumentParser(prog='swarm -c remove-worktree')
            ap.add_argument('repo')
            ap.add_argument('branch')
            ap.add_argument('--delete-branch', action='store_true')
            a = ap.parse_args(argv)
            _print(remove_worktree(a.repo, a.branch, delete_branch=a.delete_branch))
        else:
            raise KeyError(name)
    except (registry.AgentError, tmux.TmuxError) as e:
        print(f"swarm: {getattr(e, 'stderr', '') or e}", file=sys.stderr)
        return 1
    return 0


def _print_table(data):
    for r in data['repos']:
        print(f"{r['repo']}  (default: {r['default_branch'] or '?'})")
        for w in r['worktrees']:
            flags = ('plain ' if w['main'] else '') + ('dirty ' if w['dirty'] else '') + \
                ('' if w['layout'] in (True, None) else 'off-layout ') + \
                ('prunable ' if w['prunable'] else '')
            panes = ' '.join(f"{p['pane_id']}:{p['window_name']}" for p in w['panes'])
            print(f"  {w['branch'] or '(detached)':32} {w['path']}  {flags.strip()}  {panes}".rstrip())
