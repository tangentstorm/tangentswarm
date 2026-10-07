import json

import anyio
import pytest

from tangentswarm import mcp_server, shell


def tool_names():
    return [t.name for t in anyio.run(mcp_server.build_server().list_tools)]


def test_expected_tools_registered():
    names = tool_names()
    for expected in ['list_sessions', 'list_panes', 'capture_pane', 'send_keys', 'new_session',
                     'new_window', 'shell_exec', 'agent_status', 'pane_ready', 'wait_for_idle',
                     'tell_agent', 'swarm_status', 'tell_worker', 'cloud_list_sessions',
                     'cloud_send_message', 'cloud_get_latest_response', 'cloud_wait_for_response']:
        assert expected in names


def test_no_kill_tools():
    assert not [n for n in tool_names() if 'kill' in n.lower()]


def test_every_tool_has_a_scope():
    names = set(tool_names())
    assert names == set(mcp_server.TOOL_SCOPES)
    assert mcp_server.TOOL_SCOPES['shell_exec'] == 'tangentswarm:shell'
    assert mcp_server.TOOL_SCOPES['send_keys'] == 'tangentswarm:shell'
    assert mcp_server.TOOL_SCOPES['capture_pane'] == 'tangentswarm:read'
    assert mcp_server.TOOL_SCOPES['list_sessions'] == 'tangentswarm:read'


def test_shell_exec_runs_and_logs(isolated_state):
    res = anyio.run(lambda: shell.shell_exec('echo hi; echo err >&2; exit 3', cwd='/tmp', timeout=10))
    assert res['exit_code'] == 3 and res['stdout'] == 'hi\n' and 'err' in res['stderr']
    assert res['timed_out'] is False
    lines = shell.log_path().read_text().splitlines()
    entry = json.loads(lines[-1])
    assert entry['command'].startswith('echo hi') and entry['cwd'] == '/tmp'
    assert entry['exit_code'] == 3 and entry['stdout_len'] == 3 and 'stdout' not in entry
    assert oct(shell.log_path().stat().st_mode & 0o777) == '0o600'


def test_shell_exec_timeout(isolated_state):
    res = anyio.run(lambda: shell.shell_exec('sleep 30', timeout=1))
    assert res['timed_out'] is True and res['duration_sec'] < 10


def test_timeout_clamped():
    assert shell.clamp_timeout(None) == 60
    assert shell.clamp_timeout(10_000) == 600
    assert shell.clamp_timeout(-5) == 60


def test_truncation_note():
    text, cut = shell.truncate('x' * 100_000)
    assert cut and 'truncated' in text and len(text) < 70_000
