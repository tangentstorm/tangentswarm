"""swarm-promoted: Human Review below 2 -> move the gated Staging head first, then the slow step."""
import pytest

from tangentswarm import board, promoted as P

H5, H6 = '5' * 40, '6' * 40


def page(*items):
    return {'organization': {'projectV2': {'items': {'nodes': list(items),
                                                     'pageInfo': {'hasNextPage': False, 'endCursor': None}}}}}


def item(n, opt, head, state='OPEN', repo=P.REPO):
    return {'id': f'I{n}', 'fieldValueByName': {'name': 'whatever', 'optionId': opt},
            'content': {'__typename': 'PullRequest', 'number': n, 'state': state, 'headRefOid': head,
                        'repository': {'nameWithOwner': repo}}}


HR, ST = board.OPTION[board.HUMAN_REVIEW], board.OPTION[board.STAGING]


def test_read_columns_counts_open_prs_by_option_id_only():
    cols = P.read_columns(lambda q, c=None: page(item(1, HR, 'a'), item(2, HR, 'b', state='MERGED'),
                                                 item(5, ST, H5), item(9, ST, 'z', repo='other/repo')))
    assert [p['number'] for p in cols[board.HUMAN_REVIEW]] == [1]
    assert [p['number'] for p in cols[board.STAGING]] == [5]
    assert P.needs_promotion(cols)


def test_no_promotion_when_human_review_is_full_or_staging_empty():
    assert not P.needs_promotion({board.HUMAN_REVIEW: [1, 2], board.STAGING: [3]})
    assert not P.needs_promotion({board.HUMAN_REVIEW: [1]})


LOG = ("2026-11-13T19:00:00-05:00\tapply\t#5\t55555555\tPENDING\tfail=-\tcaller=x\n"
       "2026-11-13T19:00:00-05:00\tapply\t#6\t66666666\tBLOCKED\tfail=4\tcaller=x\n"
       "2026-11-13T19:00:00-05:00\tapply\t#7\t77777777\tPASS\tfail=-\tcaller=x\n"
       "2026-11-13T19:05:00-05:00\tapply\t#7\t77777777\tRELAY\tfail=9\tcaller=x\n")
GATED = P._when('2026-11-13T19:00:00-05:00')


def test_gate_times_take_the_latest_line_per_head():
    assert P.gate_times(LOG) == {('#5', '55555555'): GATED}
    assert P.gated_heads(LOG) == {('#5', '55555555')}


def test_staging_head_follows_queue_order():
    cols = {board.STAGING: [dict(number=5, head=H5, item='I5'), dict(number=6, head=H6, item='I6')]}
    assert P.staging_head(cols, [{'pr': 6}, {'pr': 5}])['number'] == 6
    assert P.staging_head(cols, [])['number'] == 5


def test_staging_order_puts_fast_track_first():
    st = [dict(number=5), dict(number=6)]
    det = {6: {'closingIssuesReferences': {'nodes': [{'labels': {'nodes': [{'name': 'fast-track'}]}}]}}}
    assert [p['number'] for p in P.staging_order(st, det, [{'pr': 5}, {'pr': 6}])] == [6, 5]


def note(kind, who, typ, when):
    key = 'createdAt' if kind == 'comments' else 'submittedAt'
    return {key: when, 'author': {'__typename': typ, 'login': who}}


def test_human_after_ignores_bots_and_older_comments():
    pr = {'comments': {'nodes': [note('comments', 'minbot', 'Bot', '2026-11-14T02:00:00Z'),
                                 note('comments', 'alice', 'User', '2026-11-13T23:59:00Z')]},
          'reviews': {'nodes': [note('reviews', 'bob', 'User', '2026-11-14T00:01:00Z')]}}
    assert P.human_after(pr, GATED) == ['bob']  # GATED is 00:00Z


def test_read_details_is_one_query_for_all_prs():
    qs = []
    out = P.read_details(lambda q: qs.append(q) or {'repository': {'p5': {'number': 5}, 'p6': None}}, [5, 6])
    assert out == {5: {'number': 5}} and len(qs) == 1 and 'p6: pullRequest(number:6)' in qs[0]


def test_merge_problem_checks_main_then_each_human_review_head():
    bad = {('m', 'h'), ('m+1', 'h')}

    def mc(scratch, a, b, msg):
        return None if (a, b) in bad else f'{a}+{b}'
    assert P.merge_problem(mc, None, 'm', [], 'h') == 'conflicts with main'
    bad = {('m+1', 'h')}
    assert P.merge_problem(mc, None, 'm', [(1, '1')], 'h') == 'conflicts with #1 in Human Review'
    assert P.merge_problem(mc, None, 'm', [(2, '2')], 'h') is None


class Scratch:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def ensure_main(self):
        return 'main'

    def ensure(self, *revs):
        pass


def detail(n, head, draft=False, base='main', body='', notes=()):
    return {'number': n, 'state': 'OPEN', 'headRefOid': head, 'isDraft': draft, 'baseRefName': base,
            'body': body, 'comments': {'nodes': list(notes)}, 'reviews': {'nodes': []}}


class G(dict):
    def __init__(self, tmp_path, details=None, queue=({'pr': 5},)):
        super().__init__()
        (tmp_path / 'hr.log').write_text(LOG)
        self.moves, self.logs = [], []
        details = details if details is not None else {5: detail(5, H5)}
        self.update(LOG=str(tmp_path / 'hr.log'), read_queue_file=lambda: list(queue),
                    gql=lambda q, c=None: {'repository': {f'p{n}': d for n, d in details.items()}},
                    ScratchRepo=Scratch, stacked_base=lambda b: 3 if b.startswith('# STACKED') else None,
                    move=lambda i, c: self.moves.append((i, c)), log=lambda *a: self.logs.append(a))


COLS = {board.HUMAN_REVIEW: [dict(number=1, head='a', item='I1')],
        board.STAGING: [dict(number=5, head=H5, item='I5'), dict(number=6, head=H6, item='I6')]}


@pytest.fixture
def clean(monkeypatch):
    from tangentswarm import flow
    monkeypatch.setattr(flow, 'merge_commit', lambda s, a, b, m: f'{a}+{b}')


def test_fast_path_moves_the_gated_head_and_logs_a_pass(tmp_path, clean):
    g = G(tmp_path)
    assert P.Promoter(g=g).fast_path(COLS) == [5]
    assert g.moves == [('I5', board.HUMAN_REVIEW)] and g.logs == [('apply', 5, '55555555', 'PASS', [])]


def test_fast_path_fills_every_free_slot(tmp_path, clean):
    cols = {board.STAGING: COLS[board.STAGING] + [dict(number=8, head='8' * 40, item='I8')]}
    g = G(tmp_path, details={5: detail(5, H5), 8: detail(8, '8' * 40)})
    (tmp_path / 'hr.log').write_text(LOG + "2026-11-13T19:00:00-05:00\tapply\t#8\t88888888\tPASS\tfail=-\tc\n")
    assert P.Promoter(g=g).fast_path(cols) == [5, 8]


def test_fast_path_refuses_each_cheap_check_failure(tmp_path, clean):
    late = note('comments', 'alice', 'User', '2026-11-14T01:00:00Z')
    for d in (detail(5, 'f' * 40), detail(5, H5, draft=True), detail(5, H5, base='other'),
              detail(5, H5, body='# STACKED ON #3'), detail(5, H5, notes=[late])):
        g = G(tmp_path, details={5: d})
        assert P.Promoter(g=g).fast_path(COLS) == [] and g.moves == []


def test_fast_path_refuses_a_conflict_with_main(tmp_path, monkeypatch):
    from tangentswarm import flow
    monkeypatch.setattr(flow, 'merge_commit', lambda s, a, b, m: None if a == 'main' and b == H5 else 'x')
    g = G(tmp_path)
    assert P.Promoter(g=g).fast_path(COLS) == [] and g.moves == []


def test_fast_path_dry_run_moves_nothing(tmp_path, clean):
    g = G(tmp_path)
    assert P.Promoter(g=g, dry_run=True).fast_path(COLS) == [5] and g.moves == []


def test_once_runs_fast_then_slow_under_the_lock(tmp_path, monkeypatch):
    order = []
    g = G(tmp_path)
    g['gql'] = lambda q, c=None: page(item(1, HR, 'a'), item(5, ST, H5))
    p = P.Promoter(g=g)
    moved = []
    monkeypatch.setattr(p, 'fast_path', lambda cols: order.append('fast') or list(moved))
    monkeypatch.setattr(p, 'slow_path', lambda: order.append('slow'))

    class L:
        def __enter__(self):
            order.append('lock')

        def __exit__(self, *a):
            return False
    monkeypatch.setattr(P.gatelock, 'gate_lock', lambda name: L())
    assert p.once() == 'promoted' and order == ['lock', 'fast', 'slow']  # nothing moved: slow path
    order.clear()
    moved.append(5)
    assert p.once() == 'promoted' and order == ['lock', 'fast']  # slot filled: no pending-promote


def test_once_busy_then_retry_skips_the_outer_read(tmp_path, monkeypatch):
    reads = []
    g = G(tmp_path)
    g['gql'] = lambda q, c=None: reads.append(1) or page(item(1, HR, 'a'), item(5, ST, H5))
    p = P.Promoter(g=g)

    class Busy:
        def __enter__(self):
            raise P.gatelock.Busy('pending-promote --apply pid 7')

        def __exit__(self, *a):
            return False
    monkeypatch.setattr(P.gatelock, 'gate_lock', lambda name: Busy())
    assert p.once() == 'busy' and len(reads) == 1
    assert p.once(retry=True) == 'busy' and len(reads) == 1


def test_once_idle_when_human_review_full(tmp_path):
    g = G(tmp_path)
    g['gql'] = lambda q, c=None: page(item(1, HR, 'a'), item(2, HR, 'b'), item(5, ST, H5))
    assert P.Promoter(g=g).once() == 'idle'


def test_slow_path_promotes_only_unless_pull_is_on(tmp_path, monkeypatch):
    from tangentswarm import flow
    calls = []
    monkeypatch.setattr(flow.Flow, 'columns', lambda self: ({}, {}))
    monkeypatch.setattr(flow.Flow, 'promote', lambda self, cols: calls.append('promote'))
    monkeypatch.setattr(flow.Flow, 'step', lambda self: calls.append('step'))
    p = P.Promoter(g=G(tmp_path))
    monkeypatch.setattr(P, 'PULL', False)
    p.slow_path()
    monkeypatch.setattr(P, 'PULL', True)
    p.slow_path()
    assert calls == ['promote', 'step']
