import subprocess

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
