"""swarm-hookgate: skip drafts and wrong columns with no REST call, debounce per head, wait for quota."""
import pytest

from tangentswarm import hookgate as hg

A, B, C, D = 'a' * 40, 'b' * 40, 'c' * 40, 'd' * 40


@pytest.fixture(autouse=True)
def cache(tmp_path, monkeypatch):
    monkeypatch.setattr(hg, 'CACHE', tmp_path)
    return tmp_path


class Board:
    """Counts GraphQL board fetches; returns the current board."""

    def __init__(self, prs):
        self.prs, self.calls = prs, 0

    def __call__(self):
        self.calls += 1
        return {k: dict(v) for k, v in self.prs.items()}


def pr(head, col='Auto Review', draft=False, state='OPEN'):
    return dict(head=head, col=col, draft=draft, state=state)


BOARD = {'1': pr(A), '2': pr(B, draft=True), '3': pr(C, col='In Progress'), '4': pr(D, col='Pending')}


def stop(sha, now, board):
    with pytest.raises(hg.Stop) as e:
        hg.check(sha, now=now, fetch=board)
    return str(e.value)


def test_skips_draft_and_in_progress_and_goes_for_gated_cols():
    b = Board(BOARD)
    assert 'draft' in stop(B, 1000, b)
    assert 'In Progress' in stop(C, 1001, b)
    assert hg.check(A, now=1002, fetch=b) == 1
    assert hg.check(D, now=1003, fetch=b) == 4
    assert b.calls == 1  # one snapshot served all four events


def test_unknown_head_refreshes_once_then_skips():
    b = Board(BOARD)
    hg.check(A, now=1000, fetch=b)
    assert 'not the head' in stop('e' * 40, 1001, b)
    assert b.calls == 2


def test_stale_snapshot_is_refreshed_and_sees_a_new_column():
    b = Board(dict(BOARD))
    assert 'In Progress' in stop(C, 1000, b)
    b.prs['3'] = pr(C, col='Auto Review')
    assert 'In Progress' in stop(C, 1000 + hg.SNAPSHOT_TTL - 1, b)  # still fresh
    assert hg.check(C, now=1000 + hg.SNAPSHOT_TTL + 1, fetch=b) == 3
    assert b.calls == 2


def test_short_sha_matches_head():
    assert hg.check(A[:8], now=1000, fetch=Board(BOARD)) == 1


def test_closed_pr_is_skipped():
    assert 'closed' in stop(A, 1000, Board({'1': pr(A, state='CLOSED')}))


def test_busy_head_is_debounced_then_checked_again():
    b = Board(BOARD)
    hg.check(A, now=1000, fetch=b)
    hg.record(A, done=False, now=1000)
    assert 'still running' in stop(A, 1000 + hg.BUSY_DEBOUNCE - 1, b)
    assert hg.check(A, now=1000 + hg.BUSY_DEBOUNCE + 1, fetch=b) == 1


def test_done_head_is_skipped_until_the_window_ends():
    b = Board(BOARD)
    hg.check(A, now=1000, fetch=b)
    hg.record(A, done=True, now=1000)
    assert 'already seen complete' in stop(A, 1500, b)
    assert hg.check(A, now=1000 + hg.DONE_DEBOUNCE + 1, fetch=b) == 1


def test_burst_of_events_reaches_rest_once():
    b = Board(BOARD)
    goes = 0
    for i in range(10):
        try:
            hg.check(A, now=1000 + i, fetch=b)
            goes += 1
        except hg.Stop:
            pass
    assert goes == 1 and b.calls == 1


def test_wait_for_quota_returns_at_once_when_enough():
    assert hg.wait_for_quota(reserve=10, rate=lambda: (500, 0), sleep=None, now=lambda: 0) == 500


def test_wait_for_quota_sleeps_until_reset():
    clock = {'t': 1000.0}
    rates = iter([(0, 1600), (4999, 4600)])
    slept = []

    def sleep(s):
        slept.append(s)
        clock['t'] += s
    left = hg.wait_for_quota(reserve=300, max_wait=3900, rate=lambda: next(rates), sleep=sleep,
                             now=lambda: clock['t'], log=lambda s: None)
    assert left == 4999 and slept == [605]


def test_wait_for_quota_refuses_past_the_limit():
    with pytest.raises(RuntimeError, match='wait limit'):
        hg.wait_for_quota(reserve=300, max_wait=60, rate=lambda: (0, 5000), sleep=None, now=lambda: 0,
                          log=lambda s: None)


def test_main_ci_last_check_records_and_debounces(monkeypatch, capsys):
    b = Board(BOARD)
    monkeypatch.setattr(hg, 'wait_for_quota', lambda: 5000)
    calls = []
    monkeypatch.setattr(hg, 'ci_last_check', lambda sha: calls.append(sha) or 0)
    real = hg.check
    monkeypatch.setattr(hg, 'check', lambda sha: real(sha, fetch=b))
    assert hg.main(['ci-last-check', A]) == 0
    assert hg.main(['ci-last-check', A]) == 1
    assert hg.main(['ci-last-check', B]) == 1
    assert calls == [A]
    out = capsys.readouterr().out
    assert 'CI complete' in out and 'already seen complete' in out and 'draft' in out
