"""swarm-launch: live memory limits, the held-launch queue, one launch per pass, the pressure flag."""
import pytest

from tangentswarm import launches as L, tick

MB, GB = L.MB, 1024 ** 3


@pytest.fixture(autouse=True)
def queue(tmp_path, monkeypatch):
    monkeypatch.setattr(L, 'QUEUE', tmp_path / 'held.json')


def show(current, high='4831838208', mx='5368709120'):
    return lambda s: f"MemoryCurrent={current}\nMemoryHigh={high}\nMemoryMax={mx}\n"


def meminfo(avail_kb):
    return lambda: f"MemTotal: 16000000 kB\nMemFree: 100 kB\nMemAvailable: {avail_kb} kB\n"


def mem(current, high=4608 * MB, mx=5120 * MB, avail=4 * GB):
    return dict(current=current, high=high, max=mx, avail=avail)


def test_memory_reads_the_live_limits_not_a_fixed_size():
    m = L.memory(show=show(3347681280), meminfo=meminfo(4000000))
    assert m == dict(current=3347681280, high=4831838208, max=5368709120, avail=4000000 * 1024)
    assert L.headroom(m) == 4831838208 - 3347681280


def test_memory_handles_unset_limits():
    m = L.memory(show=show(100, high='infinity', mx='infinity'), meminfo=meminfo(4000000))
    assert m['high'] is None and m['max'] is None and L.headroom(m) is None and L.admits(m)
    m = L.memory(show=show(100, high='infinity', mx=str(1000 * MB)), meminfo=meminfo(4000000))
    assert L.headroom(m) == 1000 * MB - 100


def test_memory_refuses_unreadable_slice():
    with pytest.raises(RuntimeError):
        L.memory(show=lambda s: "MemoryCurrent=[not set]\n", meminfo=meminfo(1))


def test_admits_needs_slice_headroom_and_host_memory():
    assert L.admits(mem(3000 * MB))
    assert not L.admits(mem(4000 * MB))                       # 608 MB under MemoryHigh
    assert not L.admits(mem(1000 * MB, avail=int(1.4 * GB)))  # host short
    assert L.admits(mem(3000 * MB, high=None))                # falls back to MemoryMax


def test_hold_is_idempotent_and_keeps_place():
    L.hold('a', 'mn-1-a', 1, now=10)
    L.hold('b', 'mn-2-b', 2, now=20)
    L.hold('a', brief='/x/1.txt', now=30)
    rows = L.read_queue()
    assert [r['branch'] for r in rows] == ['a', 'b']
    assert rows[0]['held_at'] == 10 and rows[0]['brief'] == '/x/1.txt' and rows[0]['window'] == 'mn-1-a'


def test_ordered_puts_milestone_first_then_oldest():
    rows = [dict(branch='old', pr=1, held_at=1), dict(branch='new-ptw', pr=2, held_at=5),
            dict(branch='mid', pr=3, held_at=3)]
    rank = {2: 0}.get
    assert [r['branch'] for r in L.ordered(rows, lambda pr: rank(pr, 1))] == ['new-ptw', 'old', 'mid']


class Starter:
    def __init__(self, *results):
        self.results, self.calls = list(results), []

    def __call__(self, branch, window=None):
        self.calls.append((branch, window))
        return self.results.pop(0)


def test_launch_queues_a_hold():
    st = Starter((3, dict(held=True, window='mn-1-a', pane='%1')))
    assert L.launch('a', pr=1, brief='/b/1.txt', start=st)[0] == 'held'
    assert L.read_queue()[0] == dict(branch='a', window='mn-1-a', pr=1, brief='/b/1.txt',
                                     held_at=L.read_queue()[0]['held_at'])


def test_launch_sends_the_brief_line_and_clears_the_queue():
    L.hold('a', 'mn-1-a', 1, brief='/b/1.txt')
    sent = []
    st = Starter((0, dict(window='mn-1-a', pane='%1', unit='u1')))
    what, out = L.launch('a', 'mn-1-a', 1, '/b/1.txt', start=st, send=lambda *a: sent.append(a))
    assert what == 'launched' and sent == [('%1', 'u1', '/b/1.txt')] and L.read_queue() == []


def test_launch_reports_a_failed_brief_but_counts_as_launched():
    def boom(*a):
        raise RuntimeError('no ready Claude prompt')
    what, out = L.launch('a', brief='/b', start=Starter((0, dict(pane='%1', unit='u'))), send=boom)
    assert what == 'launched' and 'no ready' in out['brief_error']


def test_brief_line_is_one_line():
    assert '\n' not in L.brief_line('/b/486.txt') and '/b/486.txt' in L.brief_line('/b/486.txt')


def test_start_next_waits_while_memory_holds():
    L.hold('a', now=1)
    st = Starter()
    what, row, detail = L.start_next(mem=lambda: mem(4500 * MB), start=st)
    assert what == 'waiting' and row['branch'] == 'a' and 'headroom 108 MB' in detail and st.calls == []


def test_start_next_starts_only_the_first_in_order():
    L.hold('old', pr=1, now=1)
    L.hold('ptw', pr=2, now=2)
    st = Starter((0, dict(window='w', pane='%2', unit='u')))
    what, row, _ = L.start_next(rank=lambda pr: 0 if pr == 2 else 1, mem=lambda: mem(1000 * MB), start=st)
    assert what == 'launched' and row['branch'] == 'ptw' and st.calls == [('ptw', None)]
    assert [r['branch'] for r in L.read_queue()] == ['old']


def test_start_next_drops_a_refusal_and_a_held_again_stays():
    L.hold('a', now=1)
    st = Starter((3, dict(held=True, window='w')))
    assert L.start_next(mem=lambda: mem(1000 * MB), start=st)[0] == 'held'
    assert len(L.read_queue()) == 1
    st = Starter((1, dict(refused='w exists but is busy')))
    assert L.start_next(mem=lambda: mem(1000 * MB), start=st)[0] == 'refused'
    assert L.read_queue() == []


# --- the swarm-tick step ------------------------------------------------------

def make_tick(prs, dry=True, data=None):
    t = tick.Tick.__new__(tick.Tick)
    t.dry, t.verbose, t.out, t.did, t.notes = dry, False, print, [], []
    t.prs, t.panes = prs, []

    class S:
        pass
    t.state = S()
    t.state.data = data if data is not None else {}
    return t


PRS = {5: dict(number=5, state='OPEN', branch='five', rank=1), 6: dict(number=6, state='OPEN', branch='six', rank=0)}


def test_tick_flags_sustained_pressure_once_it_lasts(monkeypatch):
    monkeypatch.setattr(L, 'memory', lambda: mem(4500 * MB))
    L.hold('five', now=1)
    data = {}
    t = make_tick(PRS, data=data)
    t.launch_pass(now=1000)
    assert t.notes == [] and data['pressure_since'] == 1000
    t = make_tick(PRS, data=data)
    t.launch_pass(now=1000 + tick.PRESSURE_MINUTES * 60)
    assert len(t.notes) == 1 and f'memory pressure for {tick.PRESSURE_MINUTES} min' in t.notes[0][1]
    assert 'MemoryHigh 4608 MB' in t.notes[0][1] and 'held launches: #5' in t.notes[0][1]
    assert t.did == []


def test_tick_clears_pressure_and_would_start_the_milestone_first(monkeypatch):
    monkeypatch.setattr(L, 'memory', lambda: mem(1000 * MB))
    L.hold('five', now=1)
    L.hold('six', now=2)
    data = {'pressure_since': 1}
    t = make_tick(PRS, data=data)
    t.launch_pass(now=5000)
    assert 'pressure_since' not in data and t.notes == []
    assert len(t.did) == 1 and t.did[0].startswith('would: #6 start held launch six')


def test_tick_live_starts_one_and_notes_a_refusal(monkeypatch):
    monkeypatch.setattr(L, 'memory', lambda: mem(1000 * MB))
    monkeypatch.setattr(tick, 'log', lambda line: None)
    L.hold('five', now=1)
    calls = []

    def start_next(rank, mem):
        calls.append(rank(6))
        return 'refused', L.read_queue()[0], dict(refused='busy window')
    monkeypatch.setattr(L, 'start_next', start_next)
    t = make_tick(PRS, dry=False)
    t.launch_pass(now=5000)
    assert calls == [0] and 'refused and dropped' in t.notes[0][1]


def test_tick_notes_unreadable_memory(monkeypatch):
    def boom():
        raise RuntimeError('cannot read swarm-workers.slice MemoryCurrent')
    monkeypatch.setattr(L, 'memory', boom)
    t = make_tick(PRS)
    t.launch_pass(now=1)
    assert 'cannot read worker memory' in t.notes[0][1]


# --- held launches left behind as idle scaffold shells -------------------------

def scaffold_tick(monkeypatch, tmp_path, subject, brief_text='brief'):
    from tangentswarm import brief
    monkeypatch.setattr(brief, 'BRIEF_DIR', tmp_path)
    if brief_text:
        (tmp_path / '7.txt').write_text(brief_text)
    monkeypatch.setattr(tick, 'git', lambda path, *a: subject)
    pr = dict(number=7, state='OPEN', draft=True, branch='b', head='h' * 40, col='In Progress', parked=False,
              rank=1, item='i', title='t')
    shell = dict(id='%9', window='mn-7-b', claude=False, cls='shell', pr=7, parked=False, path='/w',
                 wt=dict(top='/w', branch='b', head='h' * 40, dirty=False, ahead=0))
    t = make_tick({7: pr})
    t.panes = [shell]
    return t


def test_idle_scaffold_shell_is_queued_as_a_held_launch(monkeypatch, tmp_path):
    t = scaffold_tick(monkeypatch, tmp_path, 'Start #440')
    t.dry = False
    monkeypatch.setattr(tick, 'log', lambda line: None)
    t.no_claude(7, t.prs[7], t.panes)
    assert t.notes == [] and t.did == ['did: #7 queued held launch b (shell %9, head is the scaffold commit)']
    row = L.read_queue()[0]
    assert (row['branch'], row['window'], row['pr'], row['brief']) == ('b', 'mn-7-b', 7, str(tmp_path / '7.txt'))
    t.did = []
    t.no_claude(7, t.prs[7], t.panes)  # already queued: nothing new
    assert t.did == [] and t.notes == [] and len(L.read_queue()) == 1


def test_shell_with_real_commits_or_no_brief_is_still_judgment(monkeypatch, tmp_path):
    t = scaffold_tick(monkeypatch, tmp_path, 'Fix the title record button')
    t.no_claude(7, t.prs[7], t.panes)
    assert 'Claude exited' in t.notes[0][1] and L.read_queue() == []
    (tmp_path / '7.txt').unlink()
    t = scaffold_tick(monkeypatch, tmp_path, 'Start #440', brief_text=None)
    t.no_claude(7, t.prs[7], t.panes)
    assert 'Claude exited' in t.notes[0][1] and L.read_queue() == []
