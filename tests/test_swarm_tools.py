import json
import os
import subprocess

import pytest

from tangentswarm import local_status, rule_deps, swarm_state, tell_worker


def test_rule_deps_closure():
    guides = {
        'a.md': '---\nuses: [c.md, b.md]\n---\nbody',
        'b.md': '---\nuses:\n  - d.md\n  - a.md\n---\n',
        'c.md': 'no frontmatter',
        'd.md': '---\nuses: ["missing.md"]\n---\n',
    }
    assert rule_deps.resolve_dependencies('a.md', guides.get) == ['b.md', 'c.md', 'd.md', 'missing.md']
    assert rule_deps.uses_of('plain') == []


def test_table_width_capped():
    rows = [['jc0', 'claude', 'WORKING', 'OK', 'x' * 200]]
    out = local_status.format_swarm_table(rows)
    assert all(len(l) <= 90 for l in out.splitlines())
    assert out.splitlines()[2].endswith('…')


def test_parse_status_line():
    assert local_status.parse_status_line('READY: done step 3\nmore') == ('READY', 'done step 3')
    assert local_status.parse_status_line('working') == ('WORKING', '')
    assert local_status.parse_status_line('') is None


def test_swarm_state_deltas():
    rows = [['jc0', 'claude', 'WORKING', 'OK', 'main'], ['jc1', 'codex', 'READY', 'OK', 'up to date']]
    first = swarm_state.update_swarm_state(swarm_state.rows_to_state(rows))
    assert set(first) == {'jc0', 'jc1'} and first['jc1']['status'] == 'up to date'
    rows[0][2] = 'READY'
    second = swarm_state.update_swarm_state(swarm_state.rows_to_state(rows))
    assert set(second) == {'jc0'}


def git(cwd, *args):
    subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def control(tmp_path):
    ctl = tmp_path / 'ctl'
    (ctl / 'rules').mkdir(parents=True)
    (ctl / 'rules' / 'proving-guide.md').write_text('---\nuses: [status-guide.md]\n---\nprove things\n')
    (ctl / 'rules' / 'status-guide.md').write_text('status rules\n')
    git(ctl, 'init', '-q')
    git(ctl, '-c', 'user.name=t', '-c', 'user.email=t@t', 'add', '.')
    git(ctl, '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', 'rules')
    wdir = tmp_path / 'w0'
    (wdir / '.sci').mkdir(parents=True)
    (wdir / '.sci' / 'status-line').write_text('IDLE\n')
    (ctl / 'workers.jsonl').write_text(json.dumps({'id': 'jc0', 'dir': str(wdir), 'session': 's', 'window': '1'}) + '\n')
    return ctl, wdir


class FakeTui:
    def __init__(self, empty=True):
        self.empty = empty
        self.sent = []

    def ensure_prompt_is_empty(self):
        return self.empty

    def send_text(self, text):
        self.sent.append(text)

    def new_conversation(self):
        self.sent.append('/new')


def make_tw(ctl, tui, monkeypatch):
    tw = tell_worker.TellWorker(ctl, out=open(os.devnull, 'w'), tui_factory=lambda a, t: tui,
                                agent_detector=lambda w: 'claude')
    monkeypatch.setattr(tw, 'assert_window', lambda w: None)
    return tw


def test_tell_worker_accept_commits_after_send(control, monkeypatch):
    ctl, wdir = control
    tui = FakeTui()
    make_tw(ctl, tui, monkeypatch).run('jc0', 'accept')
    assert tui.sent and tui.sent[0].startswith('Your recent work has been accepted!')
    assert 'proving-guide.md has just been updated' in tui.sent[0]
    assert (wdir / '.sci' / 'status-line').read_text() == 'WORKING: plan next step\n'
    assert (wdir / '.sci' / 'status-guide.md').read_text() == 'status rules\n'   # the uses: closure


def test_tell_worker_is_atomic_when_prompt_busy(control, monkeypatch):
    ctl, wdir = control
    tui = FakeTui(empty=False)
    with pytest.raises(tell_worker.TellWorkerError, match='never reached empty prompt'):
        make_tw(ctl, tui, monkeypatch).run('jc0', 'accept')
    assert tui.sent == []
    assert (wdir / '.sci' / 'status-line').read_text() == 'IDLE\n'
    assert not (wdir / '.sci' / 'proving-guide.md').exists()


def test_tell_worker_assigned_uses_new_conversation(control, monkeypatch):
    ctl, wdir = control
    for f in ('goal.md', 'task.md'):
        (wdir / '.sci' / f).write_text('x')
    monkeypatch.setattr(tell_worker.TellWorker, 'git_clean', staticmethod(lambda d: True))
    tui = FakeTui()
    make_tw(ctl, tui, monkeypatch).run('jc0', 'assigned')
    assert tui.sent[0] == '/new' and tui.sent[1].startswith('/goal You have been assigned')
    assert (wdir / '.sci' / 'status-line').read_text() == 'ASSIGNED\n'


def test_tell_worker_adjust_requires_review(control, monkeypatch):
    ctl, _ = control
    with pytest.raises(tell_worker.TellWorkerError, match='review.md'):
        make_tw(ctl, FakeTui(), monkeypatch).run('jc0', 'adjust')
