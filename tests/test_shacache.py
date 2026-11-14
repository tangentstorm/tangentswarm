"""shacache: keep only answers that are final for a commit SHA."""
from tangentswarm import shacache as C


def store():
    d = {}
    return d, d.get, d.__setitem__


def test_key_is_stable_for_any_dependency_order():
    assert C.key('ci', 'h') == 'sha:ci:h'
    assert C.key('plain', 'h', 'b', 'a') == C.key('plain', 'h', 'a', 'b') != C.key('plain', 'h', 'a')


def test_memo_keeps_a_ci_pass_and_recomputes_a_failure():
    d, get, put = store()
    calls = []

    def ci(result):
        return lambda: calls.append(1) or result
    assert C.memo(get, put, 'k1', ci((False, 'running')), keep=C.ci_final) == (False, 'running')
    assert C.memo(get, put, 'k1', ci((True, 'green')), keep=C.ci_final) == (True, 'green')
    assert C.memo(get, put, 'k1', ci((False, 'never called')), keep=C.ci_final) == (True, 'green')
    assert len(calls) == 2


def test_memo_keeps_a_none_answer():
    d, get, put = store()
    calls = []
    for _ in range(2):
        assert C.memo(get, put, 'k', lambda: calls.append(1) or None) is None
    assert calls == [1]  # "no plain tip" is a valid, cached answer
