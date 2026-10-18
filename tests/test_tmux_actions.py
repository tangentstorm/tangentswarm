"""tmux_action: named tmux commands on an agent's own pane or window."""
import os
import shutil
import subprocess

import anyio
import pytest

from tangentswarm import agents, mcp_server, tmux, tmux_actions


def fake_agent(monkeypatch, agent='claude', where=None, windows='0 1 2 5'):
    """A pane %10 running `agent` in window @3 (index 2) of session $1; records commands."""
    w = {'pane_id': '%10', 'session_id': '$1', 'window_id': '@3', 'window_index': '2',
         'zoomed': '0', 'active': '1'}
    w.update(where or {})
    monkeypatch.setattr(agents, 'pane_agent', lambda t: {'pane_id': '%10', 'agent': agent})
    monkeypatch.setattr(tmux, 'display', lambda t, f: '\t'.join(
        [w['pane_id'], w['session_id'], w['window_id'], w['window_index'], w['zoomed'], w['active']]))
    ran = []

    def run_checked(argv):
        if argv[1] == 'list-windows':
            return subprocess.CompletedProcess(argv, 0, windows.replace(' ', '\n') + '\n', '')
        ran.append(argv[1:])
        return subprocess.CompletedProcess(argv, 0, '', '')
    monkeypatch.setattr(tmux, 'run_checked', run_checked)
    return ran


@pytest.mark.parametrize('action, arg, expected', [
    ('show_window', None, [['select-window', '-t', '$1:@3']]),
    ('select_pane', None, [['select-pane', '-t', '%10']]),
    ('zoom', None, [['resize-pane', '-Z', '-t', '%10']]),
    ('unzoom', None, []),                                           # not zoomed: nothing to do
    ('rename_window', 'claude-review', [['rename-window', '-t', '$1:@3', 'claude-review']]),
    ('move_window', 7, [['move-window', '-s', '$1:@3', '-t', '$1:7']]),       # free index
    ('move_window', '1', [['move-window', '-b', '-s', '$1:@3', '-t', '$1:1']]),  # taken: insert
    ('move_window', 2, []),                                         # already there
    ('set_pane_title', 'reviewing #13', [['select-pane', '-t', '%10', '-T', 'reviewing #13']]),
])
def test_action_commands(monkeypatch, action, arg, expected):
    ran = fake_agent(monkeypatch)
    out = tmux_actions.run_action('agents:5', action, arg)
    assert ran == expected
    assert out['pane_id'] == '%10' and out['agent'] == 'claude' and out['changed'] == bool(expected)


def test_zoom_states(monkeypatch):
    ran = fake_agent(monkeypatch, where={'zoomed': '1', 'active': '1'})
    tmux_actions.run_action('x', 'zoom')
    assert ran == []                                                # already zoomed on this pane
    ran = fake_agent(monkeypatch, where={'zoomed': '1', 'active': '0'})
    tmux_actions.run_action('x', 'zoom')
    assert ran == [['resize-pane', '-Z', '-t', '%10']] * 2          # unzoom the other pane, zoom ours
    ran = fake_agent(monkeypatch, where={'zoomed': '1'})
    tmux_actions.run_action('x', 'unzoom')
    assert ran == [['resize-pane', '-Z', '-t', '%10']]


@pytest.mark.parametrize('action, arg, match', [
    ('kill_pane', None, 'unknown action'),
    ('run_shell', 'id', 'unknown action'),
    ('show_window', 'x', 'takes no arg'),
    ('rename_window', None, 'needs arg'),
    ('rename_window', 'Bad Name', 'kebab-case'),
    ('rename_window', 'a;kill-server', 'kebab-case'),
    ('move_window', 'next', 'window index'),
    ('move_window', -1, '0 to 9999'),
    ('move_window', 10000, '0 to 9999'),
    ('move_window', '1;2', 'window index'),
    ('move_window', True, '0 to 9999'),
    ('set_pane_title', '', 'printable'),
    ('set_pane_title', 'x' * 65, 'printable'),
    ('set_pane_title', 'bell\x07', 'printable'),
    ('set_pane_title', 'two\nlines', 'printable'),
])
def test_bad_actions_and_args_run_nothing(monkeypatch, action, arg, match):
    ran = fake_agent(monkeypatch)
    with pytest.raises(tmux_actions.ActionError, match=match):
        tmux_actions.run_action('x', action, arg)
    assert ran == []


def test_target_that_moved_to_another_pane_runs_nothing(monkeypatch):
    ran = fake_agent(monkeypatch, where={'pane_id': '%11'})      # target re-resolves elsewhere
    with pytest.raises(tmux_actions.ActionError, match='now resolves to %11, not %10'):
        tmux_actions.run_action('agents:5', 'show_window')
    assert ran == []


def test_refuses_a_pane_without_an_agent(monkeypatch):
    ran = fake_agent(monkeypatch, agent=None)
    with pytest.raises(agents.NotAnAgentPane, match='refusing to act on it'):
        tmux_actions.run_action('x', 'show_window')
    assert ran == []


def test_tool_lists_every_action():
    doc = mcp_server.tmux_action.__doc__
    assert all(name in doc for name in tmux_actions.ACTIONS)
    assert all(name in mcp_server.INSTRUCTIONS for name in tmux_actions.ACTIONS)


def test_tool_reports_errors(monkeypatch):
    fake_agent(monkeypatch)
    with pytest.raises(Exception, match='unknown action'):
        anyio.run(lambda: mcp_server.tmux_action('x', 'detach_client'))
    out = anyio.run(lambda: mcp_server.tmux_action('x', 'move_window', 1))
    assert out['commands'] == ['move-window -b -s $1:@3 -t $1:1']


# ---------------------------------------------------------------------------
# against a real, private tmux server

@pytest.fixture
def private_tmux(tmp_path, monkeypatch):
    if not shutil.which('tmux'):
        pytest.skip('tmux not installed')
    monkeypatch.setenv('TMUX_TMPDIR', str(tmp_path))      # own socket dir, so a private server
    monkeypatch.delenv('TMUX', raising=False)              # never the server we may run inside
    env = {**os.environ}

    def t(*args):
        return subprocess.run(['tmux', '-f', '/dev/null', *args], env=env, check=True,
                              capture_output=True, text=True).stdout.strip()
    t('new-session', '-d', '-s', 'agents', '-n', 'w0', 'sleep 300')
    for name in ('w1', 'w2', 'w3'):
        t('new-window', '-d', '-t', 'agents:', '-n', name, 'sleep 300')
    monkeypatch.setattr(agents, 'pane_agent',
                        lambda target: {'pane_id': t('display', '-p', '-t', target, '#{pane_id}'),
                                        'agent': 'claude'})
    yield t
    subprocess.run(['tmux', 'kill-server'], env=env, capture_output=True)


def test_against_real_tmux(private_tmux):
    t = private_tmux

    def order():
        return t('list-windows', '-t', 'agents', '-F', '#{window_index}=#{window_name}').split()

    tmux_actions.run_action('agents:w3', 'move_window', 1)
    assert order() == ['0=w0', '1=w3', '2=w1', '3=w2']
    tmux_actions.run_action('agents:w0', 'move_window', 7)
    assert order() == ['1=w3', '2=w1', '3=w2', '7=w0']
    tmux_actions.run_action('agents:w1', 'rename_window', 'claude-review')
    assert '2=claude-review' in order()
    tmux_actions.run_action('agents:w2', 'show_window')
    assert t('display', '-p', '-t', 'agents', '#{window_name}') == 'w2'
    tmux_actions.run_action('agents:w2', 'set_pane_title', 'reviewing #13')
    assert t('display', '-p', '-t', 'agents:w2', '#{pane_title}') == 'reviewing #13'
    t('split-window', '-d', '-t', 'agents:w2', 'sleep 300')
    tmux_actions.run_action('agents:w2.1', 'zoom')
    assert t('display', '-p', '-t', 'agents:w2', '#{window_zoomed_flag} #{pane_index}') == '1 1'
    tmux_actions.run_action('agents:w2.0', 'zoom')                  # moves the zoom to pane 0
    assert t('display', '-p', '-t', 'agents:w2', '#{window_zoomed_flag} #{pane_index}') == '1 0'
    tmux_actions.run_action('agents:w2.0', 'unzoom')
    assert t('display', '-p', '-t', 'agents:w2', '#{window_zoomed_flag}') == '0'
    tmux_actions.run_action('agents:w2.1', 'select_pane')
    assert t('display', '-p', '-t', 'agents:w2', '#{pane_index}') == '1'


def test_linked_window_acts_in_the_named_session(private_tmux):
    """A window linked into two sessions has one pane id; the caller's session must win,
    not the one tmux would pick for the bare id."""
    t = private_tmux
    t('new-session', '-d', '-s', 'other', '-n', 'o0', 'sleep 300')
    t('link-window', '-d', '-s', 'agents:w2', '-t', 'other:5')
    pane = t('display', '-p', '-t', 'agents:w2', '#{pane_id}')
    assert t('display', '-p', '-t', pane, '#{session_name}') == 'other'   # tmux's own pick

    def active(session):
        return t('list-windows', '-t', session, '-F', '#{window_name}#{?window_active,*,}').split()

    out = tmux_actions.run_action('agents:w2', 'show_window')
    assert 'w2*' in active('agents') and 'o0*' in active('other')
    assert out['window'].startswith(t('display', '-p', '-t', 'agents', '#{session_id}') + ':')
    tmux_actions.run_action('agents:w2', 'move_window', 0)
    assert t('list-windows', '-t', 'agents', '-F', '#{window_index}=#{window_name}').split()[:2] == \
        ['0=w2', '1=w0']
    assert t('list-windows', '-t', 'other', '-F', '#{window_index}=#{window_name}').split() == \
        ['0=o0', '5=w2']                                                  # other session untouched
