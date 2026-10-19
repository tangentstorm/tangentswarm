"""Submitting to an agent: tell_agent, tell_worker and the tmux send helper.

The last tests run a stand-in TUI (fixtures/fake_tui.py) in a real, private tmux server.
It is started through a symlink named 'claude', so tell_agent's agent guard, the real
capture-pane -e and send-keys, and the ghost-text masking are all exercised.
"""
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tangentswarm import agents, tell_worker, tmux
from test_agents import MSG, BoxPane
from test_swarm_tools import FakeTui, control, make_tw   # noqa: F401  (control is a fixture)

FIX = Path(__file__).parent / 'fixtures'


# ---------------------------------------------------------------------------
# tell_agent and tell_worker report a failed submit

def fake_tell(monkeypatch, pane):
    monkeypatch.setattr(agents, 'require_agent_pane', lambda t: {'pane_id': '%7', 'agent': 'claude'})
    monkeypatch.setattr(agents, 'tui_for', lambda a, t: agents.ClaudeTui(
        t, capture=pane.capture, send_literal=pane.literal, send_key=pane.key, sleep=lambda s: None))


def test_tell_agent_reports_enter_presses(monkeypatch):
    pane = BoxPane(drop=1)
    fake_tell(monkeypatch, pane)
    out = agents.tell_agent('agents:1', MSG)
    assert out['enter_presses'] == 2 and out['submit_verified'] and pane.submitted == [MSG]


def test_tell_agent_new_conversation_then_text(monkeypatch):
    pane = BoxPane(drop=1)
    fake_tell(monkeypatch, pane)
    agents.tell_agent('agents:1', MSG, new_conversation=True)
    assert pane.submitted == ['/new', MSG]


def test_tell_agent_raises_when_not_submitted(monkeypatch):
    fake_tell(monkeypatch, BoxPane(drop=99))
    with pytest.raises(agents.SubmitError, match='still in claude.s input box'):
        agents.tell_agent('agents:1', MSG)


class StuckTui(FakeTui):
    """Reaches an empty prompt, but the text never leaves the box."""

    def send_text(self, text):
        raise agents.SubmitError('pending', f'❯ {text}', 't: the text is still in the box')

    def new_conversation(self):
        self.send_text('/new')


def test_tell_worker_handoff_not_submitted_changes_nothing(control, monkeypatch):   # noqa: F811
    ctl, wdir = control
    with pytest.raises(tell_worker.TellWorkerError, match='no state changes made') as e:
        make_tw(ctl, StuckTui(), monkeypatch).run('jc0', 'accept')
    assert 'still in the box' in str(e.value)
    assert (wdir / '.sci' / 'status-line').read_text() == 'IDLE\n'
    assert not (wdir / '.sci' / 'proving-guide.md').exists()


def test_tell_worker_assigned_stops_if_new_is_not_submitted(control, monkeypatch):   # noqa: F811
    ctl, wdir = control
    for f in ('goal.md', 'task.md'):
        (wdir / '.sci' / f).write_text('x')
    monkeypatch.setattr(tell_worker.TellWorker, 'git_clean', staticmethod(lambda d: True))
    with pytest.raises(tell_worker.TellWorkerError, match='no state changes made'):
        make_tw(ctl, StuckTui(), monkeypatch).run('jc0', 'assigned')
    assert (wdir / '.sci' / 'status-line').read_text() == 'IDLE\n'


# ---------------------------------------------------------------------------
# tmux.send_keys_literal

def test_long_literal_text_goes_in_pieces(monkeypatch):
    calls = []
    monkeypatch.setattr(tmux.subprocess, 'run',
                        lambda argv, **kw: calls.append(argv) or subprocess.CompletedProcess(argv, 0, '', ''))
    monkeypatch.setattr(tmux.time, 'sleep', lambda s: None)
    text = 'é' * (tmux.LITERAL_CHUNK * 2 + 5)
    tmux.send_keys_literal('%1', text)
    pieces = [c[-1] for c in calls[:-1]]
    assert [len(p) for p in pieces] == [tmux.LITERAL_CHUNK, tmux.LITERAL_CHUNK, 5]
    assert ''.join(pieces) == text and calls[-1][-1] == 'Enter'
    calls.clear()
    tmux.send_keys_literal('%1', '', enter=False)         # empty text is still one send-keys
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# against a real, private tmux server

@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    """Start fixtures/fake_tui.py as 'claude' in a private tmux server. Returns
    start(*args) -> (pane_id, events), where events() reads the TUI's event log."""
    if not shutil.which('tmux'):
        pytest.skip('tmux not installed')
    monkeypatch.setenv('TMUX_TMPDIR', str(tmp_path))      # own socket dir, so a private server
    monkeypatch.delenv('TMUX', raising=False)              # never the server we may run inside
    claude = tmp_path / 'claude'
    claude.symlink_to(os.path.realpath(sys.executable))     # comm is 'claude', a registered agent
    env = {**os.environ}
    n = [0]

    def start(*args):
        n[0] += 1
        log = tmp_path / f'events{n[0]}.log'
        log.touch()
        pane = subprocess.run(
            ['tmux', '-f', '/dev/null', 'new-session', '-d', '-P', '-F', '#{pane_id}', '-s', f'a{n[0]}',
             '-x', '100', '-y', '30', f'{claude} -I {FIX / "fake_tui.py"} {log} ' + ' '.join(args)],
            env=env, check=True, capture_output=True, text=True).stdout.strip()
        deadline = time.monotonic() + 10
        while '? for shortcuts' not in tmux.capture_pane(pane):
            assert time.monotonic() < deadline, tmux.capture_pane(pane)
            time.sleep(0.05)
        return pane, lambda: log.read_text().splitlines()
    yield start
    subprocess.run(['tmux', 'kill-server'], env=env, capture_output=True)


def fast_backoff(monkeypatch):
    monkeypatch.setattr(agents, 'SUBMIT_BACKOFF', (0.2, 0.4, 0.8, 1.6))


def test_real_tmux_submit_with_ghost_text(fake_claude):
    pane, events = fake_claude('--ghost', "'" + MSG + "'")   # the ghost reads exactly like the message
    assert agents.pane_agent(pane)['agent'] == 'claude'
    out = agents.tell_agent(pane, MSG)
    assert out['submit_verified'] and out['enter_presses'] == 1
    assert events() == [f'submit {MSG!r}']


def test_real_tmux_lost_enters_are_retried(fake_claude, monkeypatch):
    fast_backoff(monkeypatch)
    pane, events = fake_claude('--drop', '2')
    out = agents.tell_agent(pane, MSG)
    assert out['enter_presses'] == 3 and out['submit_verified']
    assert events() == ['drop', 'drop', f'submit {MSG!r}']


def test_real_tmux_unsubmitted_text_fails_loudly(fake_claude, monkeypatch):
    fast_backoff(monkeypatch)
    pane, events = fake_claude('--drop', '99')
    with pytest.raises(agents.SubmitError, match='after 4 Enter presses') as e:
        agents.tell_agent(pane, MSG)
    assert any(MSG in l for l in e.value.last_lines)
    assert events() == ['drop'] * 4


def test_real_tmux_new_conversation_multiline_and_long(fake_claude, monkeypatch):
    fast_backoff(monkeypatch)
    monkeypatch.setattr(agents, 'NEW_CONVERSATION_WAIT', 0)
    pane, events = fake_claude('--drop', '1')
    long_text = 'start ' + 'z' * (tmux.LITERAL_CHUNK * 3) + '\nend'
    agents.tell_agent(pane, 'line one\r\nline two', new_conversation=True)
    out = agents.tell_agent(pane, long_text)
    assert out['submit_verified']
    assert events() == ['drop', "submit '/new'", "submit 'line one\\nline two'", f'submit {long_text!r}']


def test_real_tmux_dialog_mid_send_gets_no_enter(fake_claude, monkeypatch):
    fast_backoff(monkeypatch)
    pane, events = fake_claude('--dialog-on-enter')
    with pytest.raises(agents.SubmitError, match='may or may not have been submitted'):
        agents.tell_agent(pane, MSG)
    assert events() == ['dialog']            # the menu never saw an Enter
