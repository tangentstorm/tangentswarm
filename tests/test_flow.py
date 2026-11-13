"""swarm-flow: methodology#56 step: promote, pull one Ready PR, advance the pull, never touch other Ready PRs."""
import subprocess

import pytest

from tangentswarm import board, flow

IP, AR, RD, ST, HR = board.IN_PROGRESS, board.AUTO_REVIEW, board.READY, board.STAGING, board.HUMAN_REVIEW


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(flow, 'STATE', tmp_path / 'pull.json')
    monkeypatch.setattr(flow, 'LOG', tmp_path / 'flow.log')
    return tmp_path


# --- pure helpers ---------------------------------------------------------------

def test_gate_verdict_pass_wait_fail():
    assert flow.gate_verdict([('1 not draft', True, 'ready')]) == ('pass', [])
    v, why = flow.gate_verdict([('5 CI green (Linux)', False, 'Linux IN_PROGRESS'),
                                ('7a screenshots current', False, 'no image in body')])
    assert v == 'wait' and len(why) == 2
    assert flow.gate_verdict([('5 CI green (Linux)', False, 'rs-linux FAILURE')])[0] == 'fail'
    assert flow.gate_verdict([('3 Codex clean on head', False, 'open thread'),
                              ('7b where-to-see line', False, 'missing')])[0] == 'fail'


def test_ready_order_milestone_then_conflicts_then_oldest():
    ready = [dict(number=9, rank=1), dict(number=4, rank=1), dict(number=7, rank=0), dict(number=2, rank=1)]
    assert [p['number'] for p in flow.ready_order(ready, {4: 0, 2: 2, 9: 0})] == [7, 4, 9, 2]


def test_event_filter_needs_no_api():
    st = {'action': 'edited', 'changes': {'field_value': {'field_node_id': board.STATUS_FIELD}}}
    other = {'action': 'edited', 'changes': {'field_value': {'field_node_id': 'PVTF_other'}}}
    assert flow.event_wants_step('projects_v2_item', st)
    assert not flow.event_wants_step('projects_v2_item', other)
    assert flow.event_wants_step('pull_request', {'action': 'closed', 'pull_request': {'merged': True}})
    assert not flow.event_wants_step('pull_request', {'action': 'closed', 'pull_request': {'merged': False}})
    assert not flow.event_wants_step('check_suite', {'action': 'completed'})


# --- stack merges in a real bare repo ------------------------------------------

class Scratch:
    def __init__(self, path):
        self.dir, self.main = str(path), None


def run(cwd, *a):
    return subprocess.run(['git', *a], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    w = tmp_path / 'w'
    w.mkdir()
    run(w, 'init', '-q', '-b', 'main')
    run(w, 'config', 'user.email', 't@example.com')
    run(w, 'config', 'user.name', 't')
    (w / 'a.txt').write_text('1\n2\n3\n')
    run(w, 'add', '.')
    run(w, 'commit', '-qm', 'base')
    main = run(w, 'rev-parse', 'HEAD')

    def branch(name, path, text):
        run(w, 'checkout', '-q', '-b', name, main)
        (w / path).write_text(text)
        run(w, 'add', '.')
        run(w, 'commit', '-qm', name)
        return run(w, 'rev-parse', 'HEAD')
    heads = dict(main=main, hr1=branch('hr1', 'b.txt', 'hr1\n'), x=branch('x', 'a.txt', '1\nX\n3\n'),
                 y=branch('y', 'a.txt', '1\nY\n3\n'), z=branch('z', 'c.txt', 'z\n'))
    return Scratch(w / '.git'), heads


def test_stack_conflict_finds_the_base_it_clashes_with(repo):
    s, h = repo
    assert flow.stack_conflict(s, h['main'], [('#1', h['hr1'])], h['z']) is None
    assert flow.stack_conflict(s, h['main'], [('#1', h['hr1']), ('#2', h['x'])], h['y']) == '#2'


def test_merge_commit_has_both_parents(repo):
    s, h = repo
    c = flow.merge_commit(s, h['x'], h['z'], 'm')
    parents = run(s.dir, 'log', '-1', '--format=%P', c).split()
    assert parents == [h['x'], h['z']]
    assert flow.merge_commit(s, h['x'], h['y'], 'm') is None


# --- the step with a fake hr-gate -------------------------------------------------

def card(n, col, head=None, rank=1, draft=False):
    return dict(number=n, state='OPEN', draft=draft, branch=f'b{n}', head=head or f'{n:040d}', title='t',
                col=col, item=f'I{n}', rank=rank, parked=False, body='')


class FakeG(dict):
    def __init__(self, prs, clean=lambda a, b: True, rows=None):
        super().__init__()
        self.prs, self.moves, self.rows = prs, [], rows or []

        class Sc:
            dir, main = '/x', 'main'

            def __enter__(s):
                return s

            def __exit__(s, *a):
                return False

            def ensure_main(s):
                return 'main'

            def ensure(s, *a):
                pass
        self.update(ScratchRepo=Sc, merge_tree_clean=lambda d, a, b: clean(a, b),
                    move=lambda item, col: self.moves.append((item, col)), origin_url=lambda: 'origin',
                    queue_from_board_or_file=lambda: [], fetch=lambda n, refresh=False: {'number': n},
                    list_hr_prs=lambda refresh=False: [], evaluate=lambda pr, **k: (self.rows, None),
                    gql=None, PARKED=set())


def make_flow(prs, dry=True, **kw):
    g = FakeG(prs, **kw)
    f = flow.Flow(dry_run=dry, out=lambda line: None, g=g, now=lambda: 10_000)
    cols = {}
    for p in prs.values():
        cols.setdefault(p['col'], []).append(p)
    f.columns = lambda: (prs, cols)
    return f, g


def test_promote_when_human_review_drops_to_one(monkeypatch):
    prs = {1: card(1, HR), 2: card(2, ST), 3: card(3, ST), 4: card(4, ST)}
    f, _ = make_flow(prs)
    f.step()
    assert f.did[0].startswith('would: pending-promote --apply (Human Review 1/2, Staging 3)')


def test_full_staging_pulls_nothing_and_touches_no_ready_pr(monkeypatch):
    prs = {1: card(1, HR), 2: card(2, HR), 3: card(3, ST), 4: card(4, ST), 5: card(5, ST), 6: card(6, RD)}
    f, g = make_flow(prs)
    monkeypatch.setattr(flow, 'stack_conflict', lambda *a: pytest.fail('no check on a Ready PR'))
    f.step()
    assert f.did == [] and g.moves == []


def test_pull_takes_the_top_fitting_ready_pr_only(monkeypatch):
    prs = {1: card(1, HR), 2: card(2, HR), 3: card(3, ST), 7: card(7, RD), 8: card(8, RD, rank=0), 9: card(9, RD)}
    f, g = make_flow(prs)
    monkeypatch.setattr(flow, 'stack_conflict', lambda s, m, bases, head: '#2' if head == prs[8]['head'] else None)
    started = []
    monkeypatch.setattr(f, 'start_pull', lambda p, s: started.append(p['number']))
    f.step()
    assert started == [7]
    assert any('#8 skipped this step: conflicts with #2' in n for n in f.notes)


def test_pr_that_does_not_merge_onto_main_goes_back_with_a_relay(monkeypatch, tmp_path):
    prs = {1: card(1, HR), 2: card(2, HR), 7: card(7, RD, rank=0), 9: card(9, RD)}
    f, g = make_flow(prs, dry=False, clean=lambda a, b: not (a == 'main' and b == prs[7]['head']))
    monkeypatch.setattr(flow, 'stack_conflict', lambda *a: None)
    monkeypatch.setattr(flow, 'Path', lambda p: tmp_path / p.replace('/', '_'))
    started = []
    monkeypatch.setattr(f, 'start_pull', lambda p, s: started.append(p['number']))
    f.step()
    assert g.moves == [('I7', IP)] and started == [9]
    assert 'merge origin/main' in (tmp_path / '_tmp_relay-7-merge-main.md').read_text()


def test_empty_ready_is_a_refill_note():
    f, _ = make_flow({1: card(1, HR), 2: card(2, HR)})
    f.step()
    assert any('Ready is empty' in n for n in f.notes)


def pull_state(n=7, head=None, started=10_000):
    flow.save_pull(dict(pr=n, head=head or f'{n:040d}', branch=f'b{n}', item=f'I{n}', work='w', started=started))


def test_advance_waits_for_ci_and_screenshots():
    pull_state()
    prs = {1: card(1, HR), 2: card(2, HR), 7: card(7, RD)}
    f, g = make_flow(prs, dry=False, rows=[('7a screenshots current', False, 'no image in body')])
    f.step()
    assert flow.load_pull()['pr'] == 7 and any('#7 pull waiting' in n for n in f.notes) and g.moves == []


def test_advance_times_out_into_in_progress(monkeypatch, tmp_path):
    monkeypatch.setattr(flow, 'Path', lambda p: tmp_path / p.replace('/', '_'))
    pull_state(started=10_000 - flow.PULL_TIMEOUT - 60)
    prs = {1: card(1, HR), 2: card(2, HR), 7: card(7, RD)}
    f, g = make_flow(prs, dry=False, rows=[('5 CI green (Linux)', False, 'Linux QUEUED')])
    monkeypatch.setattr(f, 'pull', lambda *a: None)
    f.step()
    assert g.moves == [('I7', IP)] and flow.load_pull() is None


def test_advance_hard_failure_goes_back(monkeypatch, tmp_path):
    monkeypatch.setattr(flow, 'Path', lambda p: tmp_path / p.replace('/', '_'))
    pull_state()
    prs = {1: card(1, HR), 2: card(2, HR), 7: card(7, RD)}
    f, g = make_flow(prs, dry=False, rows=[('8 no later human request', False, '1 later comment')])
    monkeypatch.setattr(f, 'pull', lambda *a: None)
    f.step()
    assert g.moves == [('I7', IP)] and '8 no later human request' in f.did[0]


def test_advance_pass_runs_hr_gate_apply(monkeypatch):
    pull_state()
    prs = {1: card(1, HR), 2: card(2, HR), 7: card(7, RD)}
    f, g = make_flow(prs, dry=False, rows=[('1 not draft', True, 'ready')])
    calls = []
    monkeypatch.setattr(f, 'tool', lambda argv: calls.append([str(a) for a in argv]) or (0, '  moved #7 to Pending'))
    monkeypatch.setattr(f, 'pull', lambda *a: None)
    f.step()
    assert calls[0][-2:] == ['--apply', '7'] and flow.load_pull() is None


def test_advance_head_moved_fails_the_pull(monkeypatch, tmp_path):
    monkeypatch.setattr(flow, 'Path', lambda p: tmp_path / p.replace('/', '_'))
    pull_state(head='f' * 40)
    prs = {1: card(1, HR), 2: card(2, HR), 7: card(7, RD)}
    f, g = make_flow(prs, dry=False)
    monkeypatch.setattr(f, 'pull', lambda *a: None)
    f.step()
    assert g.moves == [('I7', IP)] and 'head moved' in f.did[0]


def test_advance_finished_when_card_reached_staging():
    pull_state()
    f, g = make_flow({1: card(1, HR), 2: card(2, HR), 7: card(7, ST), 3: card(3, ST), 4: card(4, ST)}, dry=False)
    f.step()
    assert flow.load_pull() is None and 'pull finished' in f.did[0]


def test_main_busy_exits_75(monkeypatch):
    class B:
        def __enter__(self):
            raise flow.gatelock.Busy('hr-gate --apply 3 pid 1')

        def __exit__(self, *a):
            return False
    monkeypatch.setattr(flow.gatelock, 'gate_lock', lambda name: B())
    assert flow.main(['step']) == flow.gatelock.EXIT_BUSY
