import subprocess

import pytest

from tangentswarm import tmux


def test_parse_sessions():
    out = "agents\t$0\t4\t1\t1791409082\nscratch\t$3\t1\t0\t1791400000\n"
    rows = tmux.parse_format_output(out, tmux.SESSION_FIELDS)
    assert rows == [
        {'name': 'agents', 'id': '$0', 'windows': 4, 'attached': 1, 'created': 1791409082},
        {'name': 'scratch', 'id': '$3', 'windows': 1, 'attached': 0, 'created': 1791400000},
    ]


def test_parse_panes_types_and_tab_in_path():
    line = "\t".join(['agents', '2', 'pr6-s3sync', '0', '%5', '1', '0', 'claude',
                      '120', '40', '4242', '0', '/home/user/odd\tdir'])
    (row,) = tmux.parse_format_output(line + "\n\n", tmux.PANE_FIELDS)
    assert row['session'] == 'agents'
    assert row['window_index'] == 2 and row['window_name'] == 'pr6-s3sync'
    assert row['pane_index'] == 0 and row['pane_id'] == '%5'
    assert row['active'] is True and row['window_active'] is False
    assert row['current_command'] == 'claude'
    assert (row['width'], row['height']) == (120, 40)
    assert row['current_path'] == '/home/user/odd\tdir'


def test_format_strings_use_tmux_vars():
    assert '#{session_name}' in tmux.SESSION_FORMAT and '#{session_created}' in tmux.SESSION_FORMAT
    for var in ('pane_id', 'pane_current_command', 'pane_current_path', 'pane_width', 'pane_height'):
        assert '#{%s}' % var in tmux.PANE_FORMAT


class Recorder:
    def __init__(self, stdout=''):
        self.calls = []
        self.stdout = stdout

    def __call__(self, argv, **kw):
        assert isinstance(argv, list), 'argv lists only, never shell strings'
        assert not kw.get('shell')
        self.calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, self.stdout, '')


def test_capture_pane_argv(monkeypatch):
    rec = Recorder('hello\n')
    monkeypatch.setattr(tmux.subprocess, 'run', rec)
    assert tmux.capture_pane('agents:0') == 'hello\n'
    tmux.capture_pane('agents:0', history_lines=200)
    tmux.capture_pane('agents:0', escapes=True)
    assert rec.calls[0] == ['tmux', 'capture-pane', '-p', '-t', 'agents:0']
    assert rec.calls[1] == ['tmux', 'capture-pane', '-p', '-t', 'agents:0', '-S', '-200']
    assert rec.calls[2] == ['tmux', 'capture-pane', '-p', '-t', 'agents:0', '-e']


def test_send_keys_literal_sends_enter_separately(monkeypatch):
    rec = Recorder()
    sleeps = []
    monkeypatch.setattr(tmux.subprocess, 'run', rec)
    monkeypatch.setattr(tmux.time, 'sleep', sleeps.append)
    tmux.send_keys_literal('agents:0', 'Enter C-c; rm -rf /tmp/x')
    assert rec.calls == [
        ['tmux', 'send-keys', '-t', 'agents:0', '-l', '--', 'Enter C-c; rm -rf /tmp/x'],
        ['tmux', 'send-keys', '-t', 'agents:0', 'Enter'],
    ]
    assert sleeps == [tmux.ENTER_DELAY] and tmux.ENTER_DELAY == 0.5
    rec.calls.clear()
    tmux.send_keys_literal('agents:0', 'no enter', enter=False)
    assert len(rec.calls) == 1


def test_list_sessions_no_server(monkeypatch):
    def run(argv, **kw):
        return subprocess.CompletedProcess(argv, 1, '', 'no server running on /tmp/tmux-1000/default')
    monkeypatch.setattr(tmux.subprocess, 'run', run)
    assert tmux.list_sessions() == []


def test_new_window_argv(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(tmux.subprocess, 'run', rec)
    tmux.new_window('agents', name='w', cwd='/tmp', command='bash')
    argv = rec.calls[0]
    assert argv[:2] == ['tmux', 'new-window'] and '-d' in argv
    assert argv[argv.index('-t') + 1] == 'agents:' and argv[-1] == 'bash'


def test_prefix_keys_reads_the_targets_effective_options(monkeypatch):
    rec = Recorder('C-a\tNone\n')
    monkeypatch.setattr(tmux.subprocess, 'run', rec)
    assert tmux.prefix_keys('agents:2') == ['C-a']
    assert rec.calls == [['tmux', 'display-message', '-p', '-t', 'agents:2', '#{prefix}\t#{prefix2}']]
    rec.stdout = 'C-r\tC-s\n'
    assert tmux.prefix_keys('agents:2') == ['C-r', 'C-s']


def test_prefix_keys_without_tmux(monkeypatch):
    monkeypatch.setattr(tmux.subprocess, 'run', lambda argv, **kw: subprocess.CompletedProcess(
        argv, 1, '', 'no server running'))
    assert tmux.prefix_keys('agents:2') == []

    def no_binary(argv, **kw):
        raise FileNotFoundError('tmux')
    monkeypatch.setattr(tmux.subprocess, 'run', no_binary)
    assert tmux.prefix_keys('agents:2') == []


def test_refused_key(monkeypatch):
    asked = []
    monkeypatch.setattr(tmux, 'prefix_keys', lambda t: asked.append(t) or ['C-r'])
    for keys in ('C-b', 'c-b', ' ^B ', '^b'):
        assert tmux.refused_key(keys, 't') == 'ctrl_b'
        assert tmux.refused_key(keys, 't', literal=True) is None
    assert tmux.refused_key('\x02', 't', literal=True) == 'ctrl_b' == tmux.refused_key('x\x02', 't')
    for keys in ('C-r', 'c-R', '^R', ' ^r '):
        assert tmux.refused_key(keys, 't') == 'prefix'
        assert tmux.refused_key(keys, 't', literal=True) is None
    for keys in ('C-c', 'Escape', 'Up', 'ctrl+b ctrl+b', 'C-b C-b', 'C-r n'):
        assert tmux.refused_key(keys, 't') is None
    assert set(asked) == {'t'}


def test_refused_key_catches_the_prefix_as_a_raw_byte(monkeypatch):
    """send-keys never reaches tmux's key tables, so the byte a prefix types is the same
    stray character as the key name: refused in literal text and as a key, prefix2 too."""
    monkeypatch.setattr(tmux, 'prefix_keys', lambda t: ['C-r', 'C-Space'])
    for literal in (True, False):
        for keys in ('\x12', 'abc\x12def', '\x00', 'x\x00'):
            assert tmux.refused_key(keys, 't', literal=literal) == 'prefix'
        for keys in ('\x03', 'tab\there', 'line\nbreak', '\x1b[A'):   # other control bytes are fine
            assert tmux.refused_key(keys, 't', literal=literal) is None
    monkeypatch.setattr(tmux, 'prefix_keys', lambda t: ['M-a', 'F12', 'None'])   # no control byte
    assert tmux.refused_key('\x01', 't', literal=True) is None


@pytest.mark.parametrize('name, byte', [
    ('C-r', '\x12'), ('c-R', '\x12'), ('^R', '\x12'), ('C-a', '\x01'), ('C-@', '\x00'),
    ('C-Space', '\x00'), ('C-\\', '\x1c'), ('C-]', '\x1d'), ('C-_', '\x1f'),
    ('M-a', None), ('F1', None), ('C-1', None), ('C-M-a', None),
])
def test_ctrl_byte(name, byte):
    assert tmux._ctrl_byte(name) == byte


def test_refused_key_skips_tmux_for_literal_text(monkeypatch):
    monkeypatch.setattr(tmux, 'prefix_keys', lambda t: pytest.fail('literal text needs no lookup'))
    assert tmux.refused_key('hello', 't', literal=True) is None
