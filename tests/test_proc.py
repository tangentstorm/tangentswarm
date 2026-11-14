"""proc: every gate subprocess has a hard timeout; a hang becomes a normal failure."""
import subprocess

import pytest

from tangentswarm import proc


def test_a_hang_raises_with_check_and_says_so_on_stderr(capsys):
    with pytest.raises(RuntimeError, match='timed out after 1s: sleep 30'):
        proc.sh('sleep', '30', timeout=1)
    assert 'hr-gate: timed out after 1s: sleep 30' in capsys.readouterr().err


def test_a_hang_without_check_returns_124():
    r = proc.sh('sleep', '30', check=False, timeout=1, who='flow-status')
    assert r.returncode == proc.TIMED_OUT == 124
    assert r.stderr == 'flow-status: timed out after 1s: sleep 30'


def test_the_default_timeout_is_always_set():
    seen = {}

    def run(a, **k):
        seen.update(k)
        return subprocess.CompletedProcess(a, 0, 'ok', '')
    assert proc.sh('git', 'fetch', run=run).stdout == 'ok'
    assert seen['timeout'] == proc.SH_TIMEOUT > 0


def test_a_failure_with_check_raises_with_stderr():
    with pytest.raises(RuntimeError, match='bad thing'):
        proc.sh('sh', '-c', 'echo bad thing >&2; exit 3')
    assert proc.sh('sh', '-c', 'exit 3', check=False).returncode == 3


def test_commit_tree_makes_a_merge_commit_with_two_parents(tmp_path, monkeypatch):
    for k in ('GIT_AUTHOR_NAME', 'GIT_AUTHOR_EMAIL', 'GIT_COMMITTER_NAME', 'GIT_COMMITTER_EMAIL'):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv('HOME', str(tmp_path))  # no user identity: commit_env must supply one
    g = str(tmp_path / 'r.git')
    subprocess.run(['git', 'init', '--bare', '-q', g], check=True)
    tree = proc.sh('git', '--git-dir', g, 'mktree', check=True).stdout.strip()
    a = proc.commit_tree(g, tree, msg='a')
    b = proc.commit_tree(g, tree, msg='b')
    m = proc.commit_tree(g, tree, a, b)
    parents = proc.sh('git', '--git-dir', g, 'rev-list', '--parents', '-n1', m).stdout.split()[1:]
    assert parents == [a, b]


def test_commit_tree_timeout_is_a_runtime_error():
    def run(a, **k):
        raise subprocess.TimeoutExpired(a, k['timeout'])
    with pytest.raises(RuntimeError, match='exit 124'):
        proc.commit_tree('g', 't', 'a', 'b', run=run)
