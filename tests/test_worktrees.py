"""One worktree per agent: layout, syncing the plain checkout, .swarm.yaml, retry and removal.
Every test runs against throwaway repos: a bare origin, and a plain clone under a temp ~/ver."""
import os
import subprocess
import threading
from types import SimpleNamespace

import anyio
import pytest

from tangentswarm import mcp_server, registry, tmux, worktrees
from tangentswarm.worktrees import WorktreeError


def sh(*args, cwd):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def commit(repo, name, text='x'):
    with open(os.path.join(repo, name), 'w') as f:
        f.write(text)
    sh('git', 'add', name, cwd=repo)
    sh('git', 'commit', '-q', '-m', f'add {name}', cwd=repo)
    return sh('git', 'rev-parse', 'HEAD', cwd=repo)


SWARM_YAML = """\
worktree:
  symlink:
    - node_modules
    - .lake/packages
  copy:
    - .claude/settings.local.json
"""


@pytest.fixture
def w(tmp_path, monkeypatch):
    for k, v in {'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@example.com',
                 'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@example.com',
                 'GIT_CONFIG_GLOBAL': str(tmp_path / 'gitconfig'), 'GIT_CONFIG_NOSYSTEM': '1'}.items():
        monkeypatch.setenv(k, v)
    seed = tmp_path / 'seed'
    seed.mkdir()
    sh('git', 'init', '-q', '-b', 'main', cwd=seed)
    (seed / '.gitignore').write_text('node_modules/\n.lake/\n.claude/\n')
    (seed / '.swarm.yaml').write_text(SWARM_YAML)
    sh('git', 'add', '.', cwd=seed)
    commit(str(seed), 'README')
    origin = tmp_path / 'origin.git'
    sh('git', 'clone', '-q', '--bare', str(seed), str(origin), cwd=tmp_path)
    top = tmp_path / 'ver'
    top.mkdir()
    sh('git', 'clone', '-q', str(origin), 'proj', cwd=top)
    other = tmp_path / 'other'                  # someone else pushing to origin
    sh('git', 'clone', '-q', str(origin), str(other), cwd=tmp_path)
    monkeypatch.setenv('TANGENTSWARM_AGENT_ROOT', str(top))
    monkeypatch.setattr(worktrees, '_all_panes', lambda: [])
    return SimpleNamespace(top=str(top.resolve()), repo=str((top / 'proj').resolve()),
                           origin=str(origin), other=str(other), tmp=tmp_path)


def head(path, ref='HEAD'):
    return sh('git', 'rev-parse', ref, cwd=path)


# ---------------------------------------------------------------------------
# names

def test_branch_slug_maps_slashes_to_dashes():
    assert worktrees.branch_slug('minavo/auth-fix') == 'minavo-auth-fix'
    assert worktrees.branch_slug('a/b/c.d_e') == 'a-b-c.d_e'
    assert worktrees.worktree_path('/v', 'platform', 'x/y') == '/v/platform.x-y'


@pytest.mark.parametrize('bad', ['', '-x', '../x', 'a..b', 'a b', 'x/', 'x.lock', '$(id)',
                                 'a//b', '.hidden', 'a/.b', 'x' * 101, 'a\nb', None])
def test_bad_branch_names_rejected(bad):
    with pytest.raises(WorktreeError):
        worktrees.validate_branch(bad)


# ---------------------------------------------------------------------------
# creating worktrees and keeping the plain checkout current

def test_new_branch_is_cut_from_up_to_date_default(w):
    upstream = commit(w.other, 'news')
    sh('git', 'push', '-q', 'origin', 'main', cwd=w.other)
    wt = worktrees.ensure_worktree('proj', 'minavo/feat')
    assert wt['path'] == os.path.join(w.top, 'proj.minavo-feat')
    assert wt['source'] == 'new' and wt['default_branch'] == 'main'
    assert head(w.repo) == upstream                 # plain checkout fast-forwarded
    assert head(wt['path']) == upstream             # branch cut from it
    assert sh('git', 'branch', '--show-current', cwd=wt['path']) == 'minavo/feat'
    r = subprocess.run(['git', 'config', 'branch.minavo/feat.remote'], cwd=w.repo,
                       capture_output=True, text=True)
    assert r.returncode != 0                        # no accidental upstream of main


def test_retry_reuses_the_worktree(w):
    a = worktrees.ensure_worktree('proj', 'feat')
    commit(a['path'], 'work')
    b = worktrees.ensure_worktree(os.path.join(w.top, 'proj'), 'feat')
    assert b['path'] == a['path'] and b['source'] == 'existing'
    assert os.path.exists(os.path.join(b['path'], 'work'))


def test_concurrent_spawns_of_one_branch_serialise(w):
    out, errs = [], []

    def go():
        try:
            out.append(worktrees.ensure_worktree('proj', 'feat'))
        except Exception as e:      # pragma: no cover - reported below
            errs.append(e)
    ts = [threading.Thread(target=go) for _ in range(3)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errs
    assert sorted(o['source'] for o in out) == ['existing', 'existing', 'new']


def test_existing_local_and_origin_branches_are_resumed(w):
    sh('git', 'branch', 'local-only', cwd=w.repo)
    assert worktrees.ensure_worktree('proj', 'local-only')['source'] == 'local'
    sh('git', 'switch', '-q', '-c', 'pushed/one', cwd=w.other)
    tip = commit(w.other, 'remote-work')
    sh('git', 'push', '-q', 'origin', 'pushed/one', cwd=w.other)
    wt = worktrees.ensure_worktree('proj', 'pushed/one')
    assert wt['source'] == 'origin' and head(wt['path']) == tip
    assert sh('git', 'config', 'branch.pushed/one.remote', cwd=w.repo) == 'origin'


def test_refuses_dirty_plain_checkout(w):
    (w.tmp / 'ver' / 'proj' / 'stray.txt').write_text('x')
    with pytest.raises(WorktreeError, match='(?s)REFUSING.*uncommitted.*stray.txt'):
        worktrees.ensure_worktree('proj', 'feat')
    assert not os.path.exists(os.path.join(w.top, 'proj.feat'))


def test_refuses_plain_checkout_off_the_default_branch(w):
    sh('git', 'switch', '-q', '-c', 'wip', cwd=w.repo)
    with pytest.raises(WorktreeError, match='REFUSING.*is on wip.*must stay on main'):
        worktrees.ensure_worktree('proj', 'feat')


def test_refuses_diverged_default_branch(w):
    commit(w.repo, 'local-only')
    commit(w.other, 'upstream')
    sh('git', 'push', '-q', 'origin', 'main', cwd=w.other)
    with pytest.raises(WorktreeError, match=r'REFUSING.*diverged.*1 commits ahead, 1 behind'):
        worktrees.ensure_worktree('proj', 'feat')


def test_refuses_unpushed_commits_on_default_branch(w):
    commit(w.repo, 'local-only')
    with pytest.raises(WorktreeError, match='diverged'):
        worktrees.ensure_worktree('proj', 'feat')


def test_finds_default_branch_without_origin_head(w):
    sh('git', 'remote', 'set-head', 'origin', '-d', cwd=w.repo)
    assert worktrees.ensure_worktree('proj', 'feat')['default_branch'] == 'main'


def test_refuses_the_default_branch_itself(w):
    with pytest.raises(WorktreeError, match='tracks main'):
        worktrees.ensure_worktree('proj', 'main')


def test_slug_collision_is_refused(w):
    worktrees.ensure_worktree('proj', 'a/b')
    with pytest.raises(WorktreeError, match='maps to the same directory'):
        worktrees.ensure_worktree('proj', 'a-b')


def test_branch_checked_out_elsewhere_is_refused(w):
    sh('git', 'worktree', 'add', '-q', '-b', 'feat', os.path.join(w.top, 'proj-old'), cwd=w.repo)
    with pytest.raises(WorktreeError, match='already checked out at .*proj-old'):
        worktrees.ensure_worktree('proj', 'feat')


def test_stray_directory_at_the_target_is_refused(w):
    os.mkdir(os.path.join(w.top, 'proj.feat'))
    with pytest.raises(WorktreeError, match='not a worktree'):
        worktrees.ensure_worktree('proj', 'feat')


@pytest.mark.parametrize('repo', ['', 'nope', '..', '/tmp', 'proj/sub', '../other', 'proj.feat'])
def test_repo_must_be_a_plain_checkout_directly_under_top(w, repo):
    os.makedirs(os.path.join(w.repo, 'sub'), exist_ok=True)
    sh('git', 'worktree', 'add', '-q', '-b', 'feat', os.path.join(w.top, 'proj.feat'), cwd=w.repo)
    with pytest.raises(WorktreeError):
        worktrees.ensure_worktree(repo, 'other-branch')


def test_symlinked_repo_cannot_escape_top(w):
    os.symlink(w.other, os.path.join(w.top, 'sneaky'))
    with pytest.raises(WorktreeError, match='directly under'):
        worktrees.ensure_worktree('sneaky', 'feat')


# ---------------------------------------------------------------------------
# .swarm.yaml

def test_config_links_and_copies_and_keeps_worktree_clean(w):
    os.makedirs(os.path.join(w.repo, 'node_modules', 'left-pad'))
    os.makedirs(os.path.join(w.repo, '.lake', 'packages', 'mathlib'))
    os.makedirs(os.path.join(w.repo, '.claude'))
    with open(os.path.join(w.repo, '.claude', 'settings.local.json'), 'w') as f:
        f.write('{}')
    wt = worktrees.ensure_worktree('proj', 'feat')
    p = wt['path']
    assert sorted(wt['linked']) == ['.lake/packages', 'node_modules']
    assert wt['copied'] == ['.claude/settings.local.json'] and wt['skipped'] == []
    assert os.readlink(os.path.join(p, 'node_modules')) == os.path.join(w.repo, 'node_modules')
    assert os.path.isdir(os.path.join(p, '.lake')) and not os.path.islink(os.path.join(p, '.lake'))
    assert not os.path.islink(os.path.join(p, '.claude', 'settings.local.json'))
    # a `node_modules/` gitignore rule does not match a symlink; the managed exclude does
    assert sh('git', 'status', '--porcelain', cwd=p) == ''
    again = worktrees.ensure_worktree('proj', 'feat')
    assert sorted(again['linked']) == ['.lake/packages', 'node_modules']
    exclude = open(os.path.join(w.repo, '.git', 'info', 'exclude')).read()
    assert exclude.count(worktrees.EXCLUDE_BEGIN) == 1 and '/node_modules\n' in exclude
    # removing the worktree removes the link, not the shared directory
    worktrees.remove_worktree('proj', 'feat')
    assert os.path.isdir(os.path.join(w.repo, 'node_modules', 'left-pad'))


def test_config_missing_sources_and_escaping_sources_are_skipped(w, tmp_path):
    os.makedirs(os.path.join(w.repo, '.lake'))
    os.symlink(str(tmp_path), os.path.join(w.repo, '.lake', 'packages'))  # points outside the repo
    wt = worktrees.ensure_worktree('proj', 'feat')
    reasons = {s['path']: s['reason'] for s in wt['skipped']}
    assert reasons['.lake/packages'] == 'resolves outside the repo'
    assert reasons['node_modules'].startswith('missing')
    assert not os.path.lexists(os.path.join(wt['path'], '.lake', 'packages'))


def test_config_does_not_overwrite_tracked_files(w):
    commit(w.other, 'node_modules', 'tracked file')
    sh('git', 'push', '-q', 'origin', 'main', cwd=w.other)
    worktrees.ensure_worktree('proj', 'warmup')        # fast-forward proj first
    wt = worktrees.ensure_worktree('proj', 'feat')
    assert {'path': 'node_modules', 'reason': 'already exists in the worktree'} in wt['skipped']
    assert open(os.path.join(wt['path'], 'node_modules')).read() == 'tracked file'


@pytest.mark.parametrize('entry', ['../x', '/etc', '.git/hooks', 'a/../b', 'a/*', '!x', 'a b',
                                   './x', 'x/', '', 7])
def test_config_rejects_unsafe_paths(w, entry):
    with open(os.path.join(w.repo, '.swarm.yaml'), 'w') as f:
        f.write(f"worktree:\n  symlink: [{entry!r}]\n" if isinstance(entry, str)
                else f"worktree:\n  symlink: [{entry}]\n")
    with pytest.raises(WorktreeError, match='plain relative path'):
        worktrees.load_config(w.repo)


def test_config_rejects_unknown_keys(w):
    with open(os.path.join(w.repo, '.swarm.yaml'), 'w') as f:
        f.write("worktree:\n  run: [npm ci]\n")
    with pytest.raises(WorktreeError, match='only symlink, copy'):
        worktrees.load_config(w.repo)


def test_no_config_file_is_fine(w):
    os.remove(os.path.join(w.repo, '.swarm.yaml'))
    assert worktrees.load_config(w.repo) == {'symlink': [], 'copy': []}


# ---------------------------------------------------------------------------
# listing and removing

def test_list_worktrees_reports_layout_dirt_and_panes(w, monkeypatch):
    wt = worktrees.ensure_worktree('proj', 'x/y')
    sh('git', 'worktree', 'add', '-q', '-b', 'old', os.path.join(w.top, 'proj-old'), cwd=w.repo)
    open(os.path.join(wt['path'], 'scratch'), 'w').close()
    pane = {'session': 'agents', 'window_index': 3, 'window_name': 'claude-x-y', 'pane_id': '%7',
            'current_command': 'claude', 'current_path': os.path.join(wt['path'], 'src')}
    monkeypatch.setattr(worktrees, '_all_panes', lambda: [pane])
    data = worktrees.list_worktrees()
    [repo] = data['repos']
    assert repo['repo'] == 'proj' and repo['default_branch'] == 'main'
    by = {x['branch']: x for x in repo['worktrees']}
    assert by['main']['main'] and by['main']['layout'] is True and by['main']['dirty'] is False
    assert by['x/y']['layout'] is True and by['x/y']['dirty'] is True
    assert by['x/y']['panes'] == [{'pane_id': '%7', 'target': 'agents:3',
                                   'window_name': 'claude-x-y', 'command': 'claude'}]
    assert by['old']['layout'] is False


def test_remove_refuses_dirty_worktrees(w):
    wt = worktrees.ensure_worktree('proj', 'feat')
    open(os.path.join(wt['path'], 'untracked'), 'w').close()
    with pytest.raises(WorktreeError, match='(?s)REFUSING.*untracked'):
        worktrees.remove_worktree('proj', 'feat')
    assert os.path.isdir(wt['path'])


def test_remove_refuses_worktrees_in_use(w, monkeypatch):
    wt = worktrees.ensure_worktree('proj', 'feat')
    pane = {'session': 'agents', 'window_index': 1, 'window_name': 'claude-feat', 'pane_id': '%9',
            'current_command': 'claude', 'current_path': wt['path']}
    monkeypatch.setattr(worktrees, '_all_panes', lambda: [pane])
    with pytest.raises(WorktreeError, match=r'in use by %9 \(claude-feat'):
        worktrees.remove_worktree('proj', 'feat')
    monkeypatch.setattr(worktrees, '_all_panes', lambda: [])
    proc = subprocess.Popen(['sleep', '30'], cwd=wt['path'])
    try:
        with pytest.raises(WorktreeError, match=f'in use by pid {proc.pid}'):
            worktrees.remove_worktree('proj', 'feat')
    finally:
        proc.kill()
        proc.wait()
    assert worktrees.remove_worktree('proj', 'feat')['removed']
    assert not os.path.exists(wt['path'])


def test_remove_never_touches_the_plain_checkout(w):
    with pytest.raises(WorktreeError, match='plain checkout'):
        worktrees.remove_worktree('proj', 'main')
    with pytest.raises(WorktreeError, match='no worktree'):
        worktrees.remove_worktree('proj', 'nope')


def test_remove_deletes_only_merged_branches(w):
    worktrees.ensure_worktree('proj', 'merged')
    out = worktrees.remove_worktree('proj', 'merged', delete_branch=True)
    assert out['branch_deleted'] is True
    wt = worktrees.ensure_worktree('proj', 'unmerged')
    commit(wt['path'], 'work')
    out = worktrees.remove_worktree('proj', 'unmerged', delete_branch=True)
    assert out['removed'] and out['branch_deleted'] is False and 'not merged' in out['branch_kept']
    assert sh('git', 'branch', '--list', 'unmerged', cwd=w.repo)
    # the branch survives, so the next spawn resumes it
    assert worktrees.ensure_worktree('proj', 'unmerged')['source'] == 'local'


# ---------------------------------------------------------------------------
# spawning agents (tmux is recorded, not run)

class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, argv):
        self.calls.append(argv)
        return subprocess.CompletedProcess(
            argv, 0, stdout='agents\t5\tclaude-feat\t0\t%99\t1\t0\tclaude\t80\t24\t1\t0\t/x\n', stderr='')


@pytest.fixture
def claude_bin(tmp_path, monkeypatch):
    p = tmp_path / 'bin' / 'claude'
    p.parent.mkdir()
    p.write_text('#!/bin/sh\n')
    p.chmod(0o755)
    monkeypatch.setenv('TANGENTSWARM_AGENT_CLAUDE', str(p))
    monkeypatch.setenv('TANGENTSWARM_AGENT_MUSE', str(tmp_path / 'missing'))
    monkeypatch.setattr(tmux, 'has_session', lambda s: True)
    return str(p)


def test_spawn_starts_the_agent_in_its_worktree(w, claude_bin):
    rec = Recorder()
    out = worktrees.spawn_agent('claude', 'proj', 'minavo/feat', run=rec)
    argv = rec.calls[0]
    assert argv[argv.index('-c') + 1] == os.path.join(w.top, 'proj.minavo-feat')
    assert argv[argv.index('-n') + 1] == 'claude-minavo-feat'
    assert argv[argv.index('--') + 1:] == ['/usr/bin/env', '--', claude_bin]
    assert out['reused'] is False and out['worktree']['branch'] == 'minavo/feat'


def test_spawn_retry_returns_the_live_agent(w, claude_bin, monkeypatch):
    live = [{'pane_id': '%5', 'target': 'agents:2', 'window_name': 'claude-feat',
             'command': 'claude', 'agent': 'claude'}]
    monkeypatch.setattr(worktrees, '_live_agents', lambda path: live)
    rec = Recorder()
    out = worktrees.spawn_agent('claude', 'proj', 'feat', run=rec)
    assert out['reused'] is True and out['pane']['pane_id'] == '%5' and rec.calls == []
    live[0]['agent'] = 'muse'
    with pytest.raises(WorktreeError, match='muse is already working'):
        worktrees.spawn_agent('claude', 'proj', 'feat', run=rec)
    assert rec.calls == []


def test_spawn_checks_the_agent_before_touching_git(w, claude_bin):
    with pytest.raises(registry.AgentError, match='not installed'):
        worktrees.spawn_agent('muse', 'proj', 'feat', run=Recorder())
    with pytest.raises(registry.AgentError, match='unknown agent'):
        worktrees.spawn_agent('bash', 'proj', 'feat', run=Recorder())
    assert not os.path.exists(os.path.join(w.top, 'proj.feat'))


# ---------------------------------------------------------------------------
# the MCP tools and the CLI share the implementation

def call_tool(name, **kw):
    server = mcp_server.build_server()

    async def go():
        return await server.call_tool(name, kw)
    return anyio.run(go)


def test_mcp_tools_round_trip(w, claude_bin, monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(tmux, 'run_checked', rec)
    # there is no command parameter, so an extra one is ignored and the fixed argv still runs
    call_tool('start_agent', agent='claude', repo='proj', branch='feat', command='bash -c id')
    argv = rec.calls[0]
    assert argv[argv.index('-c') + 1] == os.path.join(w.top, 'proj.feat')
    assert argv[argv.index('--') + 1:] == ['/usr/bin/env', '--', claude_bin]
    assert not any('bash' in a or a == 'id' for a in argv)
    call_tool('list_worktrees', repo='proj')
    call_tool('remove_worktree', repo='proj', branch='feat')
    assert not os.path.exists(os.path.join(w.top, 'proj.feat'))
    with pytest.raises(Exception, match='uncommitted'):
        open(os.path.join(w.repo, 'dirt'), 'w').close()
        call_tool('start_agent', agent='claude', repo='proj', branch='feat2')


def test_cli_matches(w, claude_bin, monkeypatch, capsys):
    from tangentswarm import cli
    rec = Recorder()
    monkeypatch.setattr(tmux, 'run_checked', rec)
    assert cli.run_subcommand('start-agent', ['claude', 'proj', 'x/y', '--window', 'w1']) == 0
    argv = rec.calls[0]
    assert argv[argv.index('-c') + 1] == os.path.join(w.top, 'proj.x-y')
    assert argv[argv.index('-n') + 1] == 'w1'
    assert cli.run_subcommand('worktrees', ['proj']) == 0
    assert 'proj.x-y' in capsys.readouterr().out
    assert cli.run_subcommand('remove-worktree', ['proj', 'main']) == 1
    assert 'plain checkout' in capsys.readouterr().err
    assert cli.run_subcommand('remove-worktree', ['proj', 'x/y']) == 0
