"""swarm-advance: one step from the end of the line (promote, else pull, else MinBot pass)."""
import pytest

from tangentswarm import advance as A
from tangentswarm import board, flow, promoted

H = 'a' * 40


def receipt(head, login='chatgpt-codex-connector[bot]'):
    return {'author': {'login': login},
            'body': f"Codex Review: Didn't find any major issues. :+1:\n\n**Reviewed commit:** `{head[:10]}`"}


def codex_pr(head=H, comments=None, reviews=(), threads=(), draft=False, n=7):
    return {'number': n, 'isDraft': draft, 'headRefOid': head,
            'comments': {'nodes': list(comments if comments is not None else [receipt(head)])},
            'reviews': {'nodes': list(reviews)}, 'reviewThreads': {'nodes': list(threads)}}


def test_codex_clean_needs_a_receipt_for_the_exact_head():
    assert A.codex_clean(codex_pr())
    assert not A.codex_clean(codex_pr(comments=[receipt('b' * 40)]))
    assert not A.codex_clean(codex_pr(comments=[receipt(H, login='someone')]))
    state = {'author': {'login': 'chatgpt-codex-connector[bot]'},
             'body': '<!-- codex-review-state:v1 {"code_review":{"commit_sha":"%s","has_any_findings":false,'
                     '"has_unresolved_findings":false}} -->' % H}
    assert A.codex_clean(codex_pr(comments=[state]))


def test_codex_clean_fails_on_findings_or_an_open_codex_thread():
    codex = {'login': 'chatgpt-codex-connector[bot]'}
    finding = {'author': codex, 'commit': {'oid': H}, 'comments': {'totalCount': 2}}
    assert not A.codex_clean(codex_pr(reviews=[finding]))
    thread = {'isResolved': False, 'isOutdated': False, 'comments': {'nodes': [{'author': codex}]}}
    assert not A.codex_clean(codex_pr(threads=[thread]))
    assert A.codex_clean(codex_pr(threads=[dict(thread, isOutdated=True)]))


def test_minbot_order_puts_the_active_fast_track_first():
    prs = [dict(number=9), dict(number=4), dict(number=12, closes=[488])]
    assert [p['number'] for p in A.minbot_order(prs, 488)] == [12, 4, 9]
    assert [p['number'] for p in A.minbot_order(prs, None)] == [4, 9, 12]


class Gate(dict):
    def __init__(self, cols, details=None, rows=None):
        super().__init__()
        self.moves = []
        self.cols = cols
        self.update(gql=self.gql, move=lambda item, col: self.moves.append((item, col)),
                    fetch=lambda n, refresh=False: {'number': n},
                    list_hr_prs=lambda refresh=False: [],
                    evaluate=lambda pr, hr_peers=None, scratch=None: (rows or GOOD_ROWS, {}),
                    ScratchRepo=Scratch)
        self.details = details or {}

    def gql(self, q, **v):
        return {'repository': {f'p{n}': d for n, d in self.details.items()}}


class Scratch:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


GOOD_ROWS = [('1 not draft', True, 'ready'), ('2 mergeable onto main', True, 'clean'),
             ('3 Codex clean on head', True, 'clean'), ('4 MinBot pass on head', False, 'never'),
             ('5 CI green (Linux)', True, 'green'), ('7a screenshots current', False, 'no image')]


@pytest.fixture
def board_read(monkeypatch):
    """Advance.step reads the columns once with promoted.read_columns."""
    def use(cols):
        monkeypatch.setattr(promoted, 'read_columns', lambda gql, ft_state=None: cols)
    monkeypatch.setattr(flow, 'load_pull', lambda: None)
    return use


def run(g, dry=False):
    out, calls = [], []
    adv = A.Advance(g=g, dry_run=dry, out=out.append, minbot=lambda *a: calls.append(a))
    assert adv.step() == 0
    return out, calls


def test_promote_goes_first_and_ends_the_step(board_read, monkeypatch):
    cols = {board.HUMAN_REVIEW: [], board.STAGING: [dict(number=5, head=H, item='I5')],
            board.AUTO_REVIEW: [dict(number=7, head=H, item='I7')]}
    board_read(cols)

    def fast(self, c):
        promoted.say('moved #5 Staging -> Human Review (cheap check passed at aaaaaaaa)')
        return [5]
    monkeypatch.setattr(promoted.Promoter, 'fast_path', fast)
    out, calls = run(Gate(cols))
    assert out == ['moved #5 Staging -> Human Review (cheap check passed at aaaaaaaa)'] and calls == []


def test_pull_runs_when_no_promote_and_staging_is_short(board_read, monkeypatch):
    cols = {board.HUMAN_REVIEW: [1, 2], board.READY: [dict(number=6, head=H, item='I6', draft=False)]}
    board_read(cols)
    seen = []

    def step(self, promote=True):
        seen.append(promote)
        self.did.append('did: #6 pulled')
        return 0
    monkeypatch.setattr(flow.Flow, 'step', step)
    out, _ = run(Gate(cols))
    assert seen == [False] and out == ['pull: did: #6 pulled']


def test_minbot_pass_labels_comments_and_moves_to_ready(board_read):
    cols = {board.HUMAN_REVIEW: [1, 2], board.STAGING: [3, 4, 5],
            board.AUTO_REVIEW: [dict(number=7, head=H, item='I7')]}
    board_read(cols)
    g = Gate(cols, details={7: codex_pr()})
    out, calls = run(g)
    assert calls[0] == ('label', 'add', 7, 'MinBot-passed', '--repo', A.REPO)
    assert calls[1][:2] == ('evidence', 7) and H in calls[1] and A.pass_text(H) in calls[1]
    assert g.moves == [('I7', board.READY)]
    assert out == [f"MinBot-pass #7 at {H[:8]}: label, pass comment, Auto Review -> Ready"]


def test_minbot_pass_dry_run_moves_nothing(board_read):
    cols = {board.HUMAN_REVIEW: [1, 2], board.STAGING: [3, 4, 5],
            board.AUTO_REVIEW: [dict(number=7, head=H, item='I7')]}
    board_read(cols)
    g = Gate(cols, details={7: codex_pr()})
    out, calls = run(g, dry=True)
    assert g.moves == [] and out[0].startswith('would MinBot-pass #7')
    assert len(calls) == 2  # minbot without --apply is its own dry run


def test_minbot_pass_refuses_a_failed_gate_row(board_read):
    cols = {board.HUMAN_REVIEW: [1, 2], board.STAGING: [3, 4, 5],
            board.AUTO_REVIEW: [dict(number=7, head=H, item='I7')]}
    board_read(cols)
    rows = [r if not r[0].startswith('5 ') else ('5 CI green (Linux)', False, 'FAILURE') for r in GOOD_ROWS]
    g = Gate(cols, details={7: codex_pr()}, rows=rows)
    out, calls = run(g)
    assert calls == [] and g.moves == []
    assert out[0].startswith('nothing to do: Human Review 2/2') and '#7 5 CI green (Linux): FAILURE' in out[0]


def test_nothing_to_do_is_one_line(board_read):
    cols = {board.HUMAN_REVIEW: [1, 2], board.STAGING: [3, 4, 5],
            board.AUTO_REVIEW: [dict(number=7, head=H, item='I7'), dict(number=8, head=H, item='I8')]}
    board_read(cols)
    cols[board.AUTO_REVIEW].append(dict(number=9, head=H, item='I9'))
    g = Gate(cols, details={7: codex_pr(comments=[]), 8: codex_pr(draft=True, n=8),
                            9: dict(codex_pr(n=9), mergeable='CONFLICTING')})
    g['fetch'] = lambda n, refresh=False: pytest.fail('a skipped PR must not get the full read')
    out, calls = run(g)
    assert len(out) == 1 and calls == []
    assert out[0] == (f"nothing to do: Human Review 2/2; Staging 3/{flow.STAGING_MIN}, Ready 0; "
                      f"Auto Review: #7 not Codex-clean on {H[:8]}, #8 draft, #9 conflicts with main (GitHub mergeable)")
