import anyio

from tangentswarm import mcp_server


def tool_names():
    return [t.name for t in anyio.run(mcp_server.build_server().list_tools)]


def test_expected_tools_registered():
    names = tool_names()
    for expected in ['list_sessions', 'list_panes', 'capture_pane', 'send_keys', 'new_session',
                     'new_window', 'agent_status', 'pane_ready', 'wait_for_idle',
                     'tell_agent', 'swarm_status', 'tell_worker', 'cloud_list_sessions',
                     'cloud_send_message', 'cloud_get_latest_response', 'cloud_wait_for_response']:
        assert expected in names


def test_no_kill_tools():
    assert not [n for n in tool_names() if 'kill' in n.lower()]


def test_no_arbitrary_command_tool():
    names = tool_names()
    assert 'shell_exec' not in names and not [n for n in names if 'exec' in n.lower()]
    assert not hasattr(mcp_server, 'shell_exec')


def test_every_tool_has_a_scope():
    names = set(tool_names())
    assert names == set(mcp_server.TOOL_SCOPES)
    assert mcp_server.TOOL_SCOPES['tell_agent'] == 'tangentswarm:shell'
    assert mcp_server.TOOL_SCOPES['send_keys'] == 'tangentswarm:shell'
    assert mcp_server.TOOL_SCOPES['capture_pane'] == 'tangentswarm:read'
    assert mcp_server.TOOL_SCOPES['list_sessions'] == 'tangentswarm:read'

