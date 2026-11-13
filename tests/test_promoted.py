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


LOG = ("t\tapply\t#5\t55555555\tPENDING\tfail=-\tcaller=x\n"
       "t\tapply\t#6\t66666666\tBLOCKED\tfail=4\tcaller=x\n")


def test_gated_heads_reads_pass_and_pending_with_no_failure():
    assert P.gated_heads(LOG) == {('#5', '55555555')}


def test_staging_head_follows_queue_order():
    cols = {board.STAGING: [dict(number=5, head=H5, item='I5'), dict(number=6, head=H6, item='I6')]}
    assert P.staging_head(cols, [{'pr': 6}, {'pr': 5}])['number'] == 6
    assert P.staging_head(cols, [])['number'] == 5


class G(dict):
    def __init__(self, tmp_path, body='', head=H5, queue=({'pr': 5},)):
        super().__init__()
        (tmp_path / 'hr.log').write_text(LOG)
        self.moves, self.logs = [], []
        self.update(LOG=str(tmp_path / 'hr.log'), queue_from_board_or_file=lambda: list(queue),
                    fetch=lambda n, refresh=False: {'headRefOid': head, 'isDraft': False, 'body': body},
                    stacked_base=lambda b: 3 if b.startswith('# STACKED') else None,
                    move=lambda i, c: self.moves.append((i, c)), log=lambda *a: self.logs.append(a))


COLS = {board.HUMAN_REVIEW: [dict(number=1, head='a', item='I1')],
        board.STAGING: [dict(number=5, head=H5, item='I5'), dict(number=6, head=H6, item='I6')]}


def test_fast_path_moves_the_gated_head_first_and_logs_a_pass(tmp_path):
    g = G(tmp_path)
    assert P.Promoter(g=g).fast_path(COLS) == 5
    assert g.moves == [('I5', board.HUMAN_REVIEW)] and g.logs == [('apply', 5, '55555555', 'PASS', [])]


def test_fast_path_refuses_ungated_moved_or_stacked_heads(tmp_path):
    for g in (G(tmp_path, queue=({'pr': 6},)), G(tmp_path, head='f' * 40), G(tmp_path, body='# STACKED ON #3')):
        assert P.Promoter(g=g).fast_path(COLS) is None and g.moves == []


def test_fast_path_dry_run_moves_nothing(tmp_path):
    g = G(tmp_path)
    assert P.Promoter(g=g, dry_run=True).fast_path(COLS) == 5 and g.moves == []


def test_once_runs_fast_then_slow_under_the_lock(tmp_path, monkeypatch):
    order = []
    g = G(tmp_path)
    g['gql'] = lambda q, c=None: page(item(1, HR, 'a'), item(5, ST, H5))
    p = P.Promoter(g=g)
    monkeypatch.setattr(p, 'fast_path', lambda cols: order.append('fast'))
    monkeypatch.setattr(p, 'slow_path', lambda: order.append('slow'))

    class L:
        def __enter__(self):
            order.append('lock')

        def __exit__(self, *a):
            return False
    monkeypatch.setattr(P.gatelock, 'gate_lock', lambda name: L())
    assert p.once() == 'promoted' and order == ['lock', 'fast', 'slow']


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
