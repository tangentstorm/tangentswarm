"""Every pane swarm creates runs its command in a systemd scope in swarm-workers.slice."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tangentswarm import agents, registry, tmux

from test_registry import fake_bins, root  # noqa: F401


class Recorder:
    def __init__(self, start='', code=0):
        self.calls = []
        self.start, self.code = start, code

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        if '#{pane_start_command}' in argv:
            return subprocess.CompletedProcess(argv, self.code, self.start + '\n', 'no pane')
        return subprocess.CompletedProcess(argv, 0, '', '')


def scope_of(argv):
    """(tmux part, unit, wrapped command) of a scoped tmux argv."""
    i = argv.index('systemd-run')
    assert argv[i - 2:i] == ['/usr/bin/env', f'XDG_RUNTIME_DIR=/run/user/{os.getuid()}']
    assert argv[i:i + 5] == ['systemd-run', '--user', '--slice=swarm-workers.slice', '--scope',
                             '--expand-environment=no']
    assert argv[i + 5].startswith('--unit=') and argv[i + 6] == '--'
    assert argv[i - 3] == '--'
    return argv[:i - 3], argv[i + 5][len('--unit='):], argv[i + 7:]


@pytest.fixture
def rec(monkeypatch):
    r = Recorder()
    monkeypatch.setattr(tmux.subprocess, 'run', r)
    return r


def test_new_window_wraps_the_command_and_keeps_its_quoting(rec):
    cmd = """echo 'a  b' "$HOME" && claude --x 'y z'"""
    tmux.new_window('agents', name='pr 6/s3sync', cwd='/w', command=cmd)
    head, unit, command = scope_of(rec.calls[0])
    assert head[:2] == ['tmux', 'new-window'] and head[head.index('-n') + 1] == 'pr 6/s3sync'
    assert head[head.index('-c') + 1] == '/w' and head[head.index('-t') + 1] == 'agents:'
    assert command == ['/bin/sh', '-c', cmd]
    assert unit.startswith('swarm-pr-6-s3sync-') and len(unit) == len('swarm-pr-6-s3sync-') + 12


def test_new_session_without_a_command_scopes_the_default_shell(rec):
    tmux.new_session('agents', cwd='/w')
    head, unit, command = scope_of(rec.calls[0])
    assert head[:2] == ['tmux', 'new-session'] and unit.startswith('swarm-agents-')
    assert command == ['/bin/sh', '-c', tmux.DEFAULT_PANE_SHELL]


def test_units_are_unique(rec):
    tmux.new_window('agents', name='w', command='x')
    tmux.new_window('agents', name='w', command='x')
    assert scope_of(rec.calls[0])[1] != scope_of(rec.calls[1])[1]


def test_cli_split_raw_window_and_raw_tmux_commands_are_scoped(rec):
    tmux.split_window('s:1.0', '-h', '/w')
    head, unit, command = scope_of(rec.calls[0])
    assert head == ['tmux', 'split-window', '-h', '-t', 's:1.0', '-c', '/w']
    assert command == ['/bin/sh', '-c', tmux.DEFAULT_SHELL] and unit.startswith('swarm-s-1-0-')
    tmux.new_window_args('-t', 's', '-c', '/w')
    head, _, command = scope_of(rec.calls[1])
    assert head == ['tmux', 'new-window', '-t', 's', '-c', '/w']
    assert command == ['/bin/sh', '-c', tmux.DEFAULT_PANE_SHELL]
    tmux.run_tmux_command('s', 'neww -dn logs tail -f x.log')
    head, unit, command = scope_of(rec.calls[2])
    assert head == ['tmux', 'neww', '-dn', 'logs'] and unit.startswith('swarm-logs-')
    assert command == ['tail', '-f', 'x.log', '-t', 's']


def test_chained_and_respawn_commands(rec):
    argv = tmux.scope_spawn(['tmux', '-L', 'x', 'splitw', '-dPF#{pane_id}', 'top', '\\;',
                             'respawn-pane', '-k', '-t', '%3', 'htop', ';', 'respawnw', '-k'])
    first = argv.index('\\;')
    assert argv[:5] == ['tmux', '-L', 'x', 'splitw', '-dPF#{pane_id}']
    assert scope_of(argv[:first])[2] == ['/bin/sh', '-c', 'top']
    second = argv.index(';')
    head, unit, command = scope_of(argv[first + 1:second])
    assert head == ['respawn-pane', '-k', '-t', '%3'] and command == ['/bin/sh', '-c', 'htop']
    # a commandless respawn of a pane started with the default shell reruns it scoped
    head, unit, command = scope_of(argv[second + 1:])
    assert head == ['respawnw', '-k'] and command == ['/bin/sh', '-c', tmux.DEFAULT_PANE_SHELL]
    assert rec.calls[-1] == ['tmux', '-L', 'x', 'display-message', '-p', '#{pane_start_command}']


def test_raw_split_window_flags_match_tmux(rec):
    # tmux 3.4: split-window [-bdefhIPvZ] [-c dir] [-e env] [-F fmt] [-l size] [-t pane]; -p is
    # the old percentage; -f (full width) is boolean and must not swallow the command
    cases = [(['-bfhZ', '-l', '30%', '-e', 'K=V', '-t', 's:1', 'top'], 's:1'),
             (['-fl50%', '-t', 's:2', 'top'], 's:2'),
             (['-p', '30', '-ft', 's:3', 'top'], 's:3'),
             (['-f', 'top'], 'split-window')]
    for args, win in cases:
        head, unit, command = scope_of(tmux.scope_spawn(['tmux', 'split-window', *args]))
        assert head == ['tmux', 'split-window', *args[:-1]] and command == ['/bin/sh', '-c', 'top']
        assert unit.startswith('swarm-' + win.replace(':', '-') + '-')
    for args in (['-f', '--', 'top', 'x y'], ['-dP', '-F', '#{pane_id}', '--', 'top', 'x y']):
        head, _, command = scope_of(tmux.scope_spawn(['tmux', 'splitw', *args]))
        assert head == ['tmux', 'splitw', *args[:-3]] and command == ['top', 'x y']


def test_commandless_respawn_of_a_legacy_unscoped_pane_scopes_its_own_command(monkeypatch):
    rec = Recorder('claude --resume "a b" "\\\\$HOME" \\~/x')
    monkeypatch.setattr(tmux.subprocess, 'run', rec)
    tmux.run_tmux_command('agents', 'respawn-pane -k -t agents:3')
    assert rec.calls[0] == ['tmux', 'display-message', '-p', '-t', 'agents:3', '#{pane_start_command}']
    head, unit, command = scope_of(rec.calls[1])
    assert head == ['tmux', 'respawn-pane', '-k', '-t', 'agents:3'] and unit.startswith('swarm-agents-3-')
    assert command == ['claude', '--resume', 'a b', '$HOME', '~/x']


def test_commandless_respawn_of_a_single_string_or_default_shell_pane(monkeypatch):
    for start, command in (('"sleep 1; echo \\"q\\""', ['/bin/sh', '-c', 'sleep 1; echo "q"']),
                           ('', ['/bin/sh', '-c', tmux.DEFAULT_PANE_SHELL])):
        monkeypatch.setattr(tmux.subprocess, 'run', Recorder(start))
        assert scope_of(tmux.scope_spawn(['tmux', 'respawnw', '-k', '-t', 's:1']))[2] == command


def test_commandless_respawn_of_a_scoped_pane_gets_a_fresh_unit(monkeypatch):
    old = tmux.scope_argv('w', ['/usr/bin/env', '--', '/bin/claude'])
    monkeypatch.setattr(tmux.subprocess, 'run', Recorder(' '.join(old)))
    argv = tmux.scope_spawn(['tmux', 'respawnp', '-k', '-t', '%1'])
    head, unit, command = scope_of(argv)
    assert command == ['/usr/bin/env', '--', '/bin/claude'] and argv.count('systemd-run') == 1
    assert unit.startswith('swarm-1-') and f'--unit={unit}' != old[7]


def test_parse_start_command_decodes_tmux_quoting_and_refuses_the_unknown():
    p = tmux.parse_start_command
    assert p("a '' \\# \\; 'x\"y' \"\\$A \\\\\\$B \\\\$1 \\t\\101\"") == \
        ['a', '', '#', ';', 'x"y', '$A \\$B \\$1 \tA']
    for bad in ('"open', "'open", 'a\\q', 'a\\'):
        with pytest.raises(ValueError):
            p(bad)


def test_commandless_respawn_fails_closed(monkeypatch):
    monkeypatch.setattr(tmux.subprocess, 'run', Recorder('x', code=1))
    with pytest.raises(tmux.TmuxError):
        tmux.scope_spawn(['tmux', 'respawn-pane', '-k', '-t', '%9'])
    monkeypatch.setattr(tmux.subprocess, 'run', Recorder('claude "unterminated'))
    with pytest.raises(tmux.TmuxError, match='cannot scope'):
        tmux.scope_spawn(['tmux', 'respawn-pane', '-k', '-t', '%9'])


def test_non_spawn_commands_are_untouched():
    for argv in (['tmux', 'send-keys', '-t', 'x', 'new-window foo'], ['tmux', 'kill-pane', '-t', '%1'],
                 ['tmux', 'list-panes', '-a'], ['ps', 'new-window']):
        assert tmux.scope_spawn(argv) == argv


def test_start_agent_through_the_real_runner_is_scoped(fake_bins, root, monkeypatch, rec):  # noqa: F811
    monkeypatch.setattr(tmux, 'has_session', lambda s: True)
    registry.launch_agent('claude', 'proj', window_name='swarm-selftest')
    head, unit, command = scope_of(rec.calls[0])
    assert head[:2] == ['tmux', 'new-window'] and head[head.index('-n') + 1] == 'swarm-selftest'
    assert command == ['/usr/bin/env', '--', fake_bins['claude']]
    assert unit.startswith('swarm-swarm-selftest-')


FAKE_SYSTEMD_RUN = '''#!/bin/sh
printf '%s\\n' "XDG_RUNTIME_DIR=$XDG_RUNTIME_DIR" "$@" > "$SCOPE_LOG"
while [ "$1" != -- ]; do shift; done
shift
exec "$@"
'''


def test_tell_agent_still_detects_claude_in_a_scoped_pane(fake_bins, root, tmp_path, monkeypatch):  # noqa: F811
    if not shutil.which('tmux'):
        pytest.skip('tmux not installed')
    claude = Path(fake_bins['claude'])
    claude.write_text('#!/bin/sh\nwhile :; do sleep 1; done\n')
    sbin = tmp_path / 'sbin'
    sbin.mkdir()
    (sbin / 'systemd-run').write_text(FAKE_SYSTEMD_RUN)
    (sbin / 'systemd-run').chmod(0o755)
    monkeypatch.setenv('PATH', f"{sbin}:{os.environ['PATH']}")
    monkeypatch.setenv('SCOPE_LOG', str(tmp_path / 'scope.log'))
    monkeypatch.setenv('TMUX_TMPDIR', str(tmp_path))
    monkeypatch.delenv('TMUX', raising=False)
    try:
        out = registry.launch_agent('claude', 'proj', session='scoped')
        target = f"scoped:{out['window_name']}"
        for _ in range(50):
            info = agents.pane_agent(target)
            if info['agent']:
                break
            subprocess.run(['sleep', '0.1'])
        assert info['agent'] == 'claude'
        log = (tmp_path / 'scope.log').read_text().splitlines()
        assert log[0] == f'XDG_RUNTIME_DIR=/run/user/{os.getuid()}'
        assert log[1:5] == ['--user', '--slice=swarm-workers.slice', '--scope', '--expand-environment=no']
        assert log[5].startswith('--unit=swarm-claude-proj-')
        assert log[6:] == ['--', '/usr/bin/env', '--', str(claude)]
    finally:
        subprocess.run(['tmux', 'kill-server'], capture_output=True)


def test_legacy_pane_respawn_reruns_its_exact_argv_scoped(tmp_path, monkeypatch):
    if not shutil.which('tmux'):
        pytest.skip('tmux not installed')
    sbin = tmp_path / 'sbin'
    sbin.mkdir()
    (sbin / 'systemd-run').write_text(FAKE_SYSTEMD_RUN)
    (sbin / 'systemd-run').chmod(0o755)
    monkeypatch.setenv('PATH', f"{sbin}:{os.environ['PATH']}")
    monkeypatch.setenv('TMUX_TMPDIR', str(tmp_path))
    monkeypatch.delenv('TMUX', raising=False)
    legacy = ['/bin/sh', '-c', 'echo "a b" $HOME "$1" ${HOME}; exec sleep 300', 'arg0', "it's", 'x  y',
              '~/z', '', '#', 'a;b', '%y', '$', 'a\\b', '\\$1', '\\$HOME', 'tab\there', 'q"r', 'é']
    single = 'sleep 300; echo "q $HOME" \\ $1'
    raw = ['tmux', '-f', '/dev/null']
    try:
        subprocess.run([*raw, 'new-session', '-d', '-s', 'old', '-n', 'multi', '--', *legacy], check=True)
        subprocess.run([*raw, 'new-window', '-d', '-t', 'old:', '-n', 'single', single], check=True)
        for window, expected in (('multi', legacy), ('single', [single])):
            log = tmp_path / f'{window}.log'
            tmux.run_checked(['tmux', 'respawn-pane', '-k', '-e', f'SCOPE_LOG={log}', '-t', f'old:{window}'])
            for _ in range(50):
                if log.exists() and log.read_text().endswith('\n'):
                    break
                subprocess.run(['sleep', '0.1'])
            lines = log.read_text().split('\n')[:-1]
            assert lines[5].startswith(f'--unit=swarm-old-{window}-')
            got = lines[7:]
            assert got == (expected if len(expected) > 1 else ['/bin/sh', '-c', expected[0]])
    finally:
        subprocess.run(['tmux', 'kill-server'], capture_output=True)
