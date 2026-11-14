"""mainmerge: on a new main, only the merge-with-main probe; relay only the PRs that conflict."""
from tangentswarm import board, mainmerge as M

MAIN = 'm' * 40
A, B = 'a' * 40, 'b' * 40
ST = [dict(number=5, head=A, item='I5'), dict(number=6, head=B, item='I6')]


def test_conflicts_probe_once_per_main_and_head():
    calls, cache = [], {}

    def clean(m, h):
        calls.append(h)
        return h == A
    assert [p['number'] for p in M.conflicts(ST, MAIN, clean, cache)] == [6]
    assert [p['number'] for p in M.conflicts(ST, MAIN, clean, cache)] == [6]
    assert calls == [A, B]  # the second pass is all cache hits


def test_recheck_moves_and_relays_only_the_conflicting_pr(tmp_path, monkeypatch):
    monkeypatch.setattr(M, 'RELAY_DIR', tmp_path)
    moves, said, sent = [], [], []
    out = M.recheck(ST, MAIN, lambda m, h: h == A, lambda i, c: moves.append((i, c)), said.append,
                    details={6: {'headRefName': 'ptw/x'}}, cache_path=tmp_path / 'c.json',
                    send=lambda pr, text: sent.append((pr, text)) or 'sent')
    assert out == [6] and moves == [('I6', board.IN_PROGRESS)]
    (relay,) = tmp_path.glob('relay-6-mergemain-*.md')
    text = relay.read_text()
    assert 'git merge origin/main' in text and 'NO rebase' in text and 'ptw/x' in text
    assert sent == [({'number': 6, 'branch': 'ptw/x'}, text)]
    assert 'merge-main relay' in said[-1] and said[-1].endswith('sent')


def test_recheck_all_clean_moves_nothing(tmp_path):
    moves, said = [], []
    assert M.recheck(ST, MAIN, lambda m, h: True, moves.append, said.append, cache_path=tmp_path / 'c') == []
    assert moves == [] and 'still merges cleanly' in said[-1]


def test_recheck_dry_run_moves_and_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(M, 'RELAY_DIR', tmp_path)
    moves = []
    assert M.recheck(ST, MAIN, lambda m, h: False, moves.append, lambda s: None, dry=True,
                     cache_path=tmp_path / 'c') == [5, 6]
    assert moves == [] and not list(tmp_path.glob('relay-*'))


def test_deliver_holds_without_a_pane_or_when_the_prompt_is_busy():
    pr = dict(number=6, branch='ptw/x')
    assert M.deliver(pr, 't', find=lambda p: None, tell=None) == 'held: no live Claude pane'

    def busy(pane, text):
        raise RuntimeError('never reached an empty prompt')
    assert M.deliver(pr, 't', find=lambda p: '%9', tell=busy).startswith('held: never reached')
    got = []
    assert M.deliver(pr, 'hi\n', find=lambda p: '%9', tell=lambda pane, text: got.append((pane, text))) == 'sent'
    assert got == [('%9', 'hi')]


def test_find_pane_matches_window_or_worktree_and_needs_claude(monkeypatch):
    from tangentswarm import tick
    monkeypatch.setattr(tick, 'is_claude', lambda pid, cmd: cmd == 'claude')
    rows = ['%1\tptw-6-x\tbash\t/x', '%2\tptw-6-x\tclaude\t/x', '%3\tother\tclaude\t/y']
    assert M.find_pane(dict(number=6, branch='ptw/x'), list_panes=lambda: rows) == '%2'
    assert M.find_pane(dict(number=7, branch='ptw/z'), list_panes=lambda: rows) is None
