"""gitpush: push from the scratch repo (objects via alternates), surface git's stderr."""
import os
import subprocess

import pytest

from tangentswarm import gitpush


def run(*a, cwd=None):
    return subprocess.run(a, cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


class Scratch:
    """Like hr-gate's ScratchRepo: a bare repo that borrows a checkout's objects."""
    def __init__(self, path, objects):
        self.dir = str(path)
        run('git', 'init', '--bare', '--quiet', self.dir)
        (path / 'objects' / 'info' / 'alternates').write_text(objects + '\n')
        self.ensured = []

    def ensure(self, *revs):
        self.ensured += revs

    def git(self, *a, check=True):
        return subprocess.run(['git', '--git-dir', self.dir, *a], capture_output=True, text=True, check=check)


@pytest.fixture
def repos(tmp_path, monkeypatch):
    for k, v in dict(GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@x', GIT_COMMITTER_NAME='t',
                     GIT_COMMITTER_EMAIL='t@x').items():
        monkeypatch.setenv(k, v)
    work = tmp_path / 'work'
    run('git', 'init', '--quiet', '-b', 'main', str(work))
    run('git', 'commit', '--quiet', '--allow-empty', '-m', 'a', cwd=work)
    a = run('git', 'rev-parse', 'HEAD', cwd=work)
    run('git', 'commit', '--quiet', '--allow-empty', '-m', 'b', cwd=work)
    b = run('git', 'rev-parse', 'HEAD', cwd=work)
    origin = tmp_path / 'origin.git'
    run('git', 'init', '--bare', '--quiet', str(origin))
    run('git', 'push', '--quiet', str(origin), f'{a}:refs/heads/pr', cwd=work)
    scratch = Scratch(tmp_path / 'scratch', str(work / '.git' / 'objects'))
    elsewhere = tmp_path / 'elsewhere'  # a cwd with no git repo at all, like the daemon's
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    return scratch, str(origin), a, b


def test_force_with_lease_push_works_from_any_cwd(repos):
    scratch, origin, a, b = repos
    gitpush.push(scratch, origin, [f'{b}:refs/heads/pr'], lease=('refs/heads/pr', a), need=[b])
    assert run('git', '--git-dir', origin, 'rev-parse', 'refs/heads/pr') == b
    assert scratch.ensured == [b]


def test_a_stale_lease_raises_with_gits_own_message(repos):
    scratch, origin, a, b = repos
    with pytest.raises(gitpush.PushError, match='stale info'):
        gitpush.push(scratch, origin, [f'{b}:refs/heads/pr'], lease=('refs/heads/pr', b), what='reset pr')
    assert run('git', '--git-dir', origin, 'rev-parse', 'refs/heads/pr') == a


def test_dry_run_changes_nothing(repos):
    scratch, origin, a, b = repos
    gitpush.push(scratch, origin, [f'{b}:refs/heads/pr'], lease=('refs/heads/pr', a), dry_run=True)
    assert run('git', '--git-dir', origin, 'rev-parse', 'refs/heads/pr') == a


def test_delete_needs_no_commit(repos):
    scratch, origin, a, b = repos
    gitpush.push(scratch, origin, [':refs/heads/pr'])
    assert not os.path.exists(os.path.join(origin, 'refs', 'heads', 'pr'))
    assert scratch.ensured == []
