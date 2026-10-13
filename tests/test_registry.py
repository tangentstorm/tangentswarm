"""The agent allowlist, cwd confinement and the 'type only into agents' guard."""
import os
import subprocess
from pathlib import Path

import anyio
import pytest

from tangentswarm import agents, mcp_server, registry, tmux

FIX = Path(__file__).parent / 'fixtures'


# ---------------------------------------------------------------------------
# fixtures

@pytest.fixture
def root(tmp_path, monkeypatch):
    r = tmp_path / 'ver'
    (r / 'proj').mkdir(parents=True)
    (tmp_path / 'outside').mkdir()
    monkeypatch.setenv('TANGENTSWARM_AGENT_ROOT', str(r))
    return r


@pytest.fixture
def fake_bins(tmp_path, monkeypatch):
    """Point every installed-agent lookup at throwaway executables (muse, claude only)."""
    bins = {}
    for name in ('muse', 'claude'):
        p = tmp_path / 'bin' / name
        p.parent.mkdir(exist_ok=True)
        p.write_text('#!/bin/sh\n')
        p.chmod(0o755)
        bins[name] = str(p)
        monkeypatch.setenv(f'TANGENTSWARM_AGENT_{name.upper()}', str(p))
    for name in ('grok', 'gemini', 'codex'):
        monkeypatch.setenv(f'TANGENTSWARM_AGENT_{name.upper()}', str(tmp_path / 'missing' / name))
    return bins


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, argv):
        self.calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout='agents\t5\tmuse-proj\t0\t%99\t1\t0\tmuse\t80\t24\t1\t0\t/x\n',
                                           stderr='')


def call_tool(name, **kw):
    """Call a tool through the MCP server, as a client would."""
    server = mcp_server.build_server()

    async def go():
        return await server.call_tool(name, kw)
    return anyio.run(go)


# ---------------------------------------------------------------------------
# start_agent allowlist, no free commands, cwd confinement

def test_launch_argv_is_fixed(fake_bins):
    assert registry.launch_argv('muse') == [fake_bins['muse'], '--trust-workspace']
    assert registry.launch_argv('claude') == ['/usr/bin/env', '--', fake_bins['claude']]


def test_unknown_agent_rejected(fake_bins, root):
    with pytest.raises(registry.AgentError, match='unknown agent'):
        registry.launch_agent('bash', 'proj', run=Recorder())
    with pytest.raises(registry.AgentError, match='unknown agent'):
        registry.launch_agent('rm -rf /', 'proj', run=Recorder())


def test_arbitrary_command_rejected_by_the_tool(fake_bins, root, monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(tmux, 'run_checked', rec)
    monkeypatch.setattr(tmux, 'has_session', lambda s: True)
    # agent is an enum, so anything else is refused before tmux runs
    with pytest.raises(Exception):
        call_tool('start_agent', agent='bash', repo='proj', branch='x')
    with pytest.raises(Exception):
        call_tool('new_window', session='agents', command='bash')
    with pytest.raises(Exception):
        call_tool('new_session', name='x', command='bash')
    assert rec.calls == []
    # an extra `command` argument is ignored: see test_worktrees.test_mcp_tools_round_trip


def test_not_installed_agent_rejected(fake_bins, root):
    with pytest.raises(registry.AgentError, match='not installed'):
        registry.launch_agent('grok', 'proj', run=Recorder())


@pytest.mark.parametrize('cwd', ['/', '/tmp', '..', '../outside', 'proj/../../outside', '', '   '])
def test_cwd_outside_root_rejected(fake_bins, root, cwd):
    with pytest.raises(registry.AgentError):
        registry.launch_agent('muse', cwd, run=Recorder())


def test_cwd_root_itself_and_missing_dirs_rejected(root):
    with pytest.raises(registry.AgentError):
        registry.validate_cwd(str(root))
    with pytest.raises(registry.AgentError, match='existing directory'):
        registry.validate_cwd('nope')


def test_symlink_escape_rejected(fake_bins, root, tmp_path):
    os.symlink(tmp_path / 'outside', root / 'sneaky')
    with pytest.raises(registry.AgentError, match='not inside'):
        registry.validate_cwd('sneaky')
    os.symlink(root / 'proj', root / 'alias')          # a symlink that stays inside is fine
    assert registry.validate_cwd('alias') == str((root / 'proj').resolve())


def test_bad_names_rejected(fake_bins, root):
    with pytest.raises(registry.AgentError):
        registry.launch_agent('muse', 'proj', session='a:b', run=Recorder())
    for bad in ['Has Caps', 'semi;colon', 'x' * 41, '-lead', 'a..b', '$(id)']:
        with pytest.raises(registry.AgentError):
            registry.launch_agent('muse', 'proj', window_name=bad, run=Recorder())


def test_start_agent_execs_argv_without_a_shell(fake_bins, root, monkeypatch):
    monkeypatch.setattr(tmux, 'has_session', lambda s: True)
    rec = Recorder()
    out = registry.launch_agent('muse', str(root / 'proj'), window_name='swarm-selftest', run=rec)
    argv = rec.calls[0]
    assert argv[:2] == ['tmux', 'new-window'] and '-d' in argv
    assert argv[argv.index('-c') + 1] == str((root / 'proj').resolve())
    assert argv[argv.index('-n') + 1] == 'swarm-selftest'
    assert argv[argv.index('--') + 1:] == [fake_bins['muse'], '--trust-workspace']
    assert out['pane']['pane_id'] == '%99'
    # a flag-less agent is run through env so tmux never falls back to `sh -c`
    rec = Recorder()
    monkeypatch.setattr(tmux, 'has_session', lambda s: False)
    out = registry.launch_agent('claude', 'proj', session='scratch', run=rec)
    argv = rec.calls[0]
    assert argv[:2] == ['tmux', 'new-session'] and argv[argv.index('-s') + 1] == 'scratch'
    assert argv[argv.index('--') + 1:] == ['/usr/bin/env', '--', fake_bins['claude']]
    assert out['window_name'] == 'claude-proj'


# ---------------------------------------------------------------------------
# recognising agents in a pane

def test_classify_process():
    c = registry.classify_process
    assert c('claude', 'claude Task: fix it') == 'claude'
    assert c('muse-bin-1.4.3-', '/home/m/.local/bin/muse-bin-1.4.3-R5018.1 --trust-workspace hi') == 'muse'
    assert c('grok-linux-x86_', '/home/m/.grok/bin/grok') == 'grok'
    assert c('node', 'node /home/m/.npm-global/bin/codex') == 'codex'
    assert c('node', 'node /usr/local/bin/gemini') == 'gemini'
    # a shell, or a pager whose *argument* is named like an agent, is not an agent
    assert c('bash', '-bash') is None
    assert c('less', 'less claude') is None
    assert c('bash', '/bin/bash /tmp/start-muse.sh claude codex') is None


def test_only_foreground_processes_count():
    procs = [('1', 'Ss', 'bash', '-bash'), ('2', 'T', 'claude', 'claude')]   # claude suspended (C-z)
    assert registry.agent_from_processes(procs) == (None, None)
    procs = [('1', 'Ss', 'bash', '-bash'), ('2', 'S+', 'start-x', '/bin/bash /tmp/start-x.sh'),
             ('3', 'Sl+', 'claude', 'claude do things')]
    assert registry.agent_from_processes(procs) == ('claude', '3')


def fake_pane(monkeypatch, procs, pane_id='%11', dead='0'):
    monkeypatch.setattr(tmux, 'display', lambda t, f: f'{pane_id}\t/dev/pts/8\t{dead}\tagents\t0\t0')
    monkeypatch.setattr(agents, 'foreground_processes', lambda tty: procs)


BASH = [('339507', 'Ss+', 'bash', '-bash')]
CLAUDE = [('1', 'Ss', 'bash', '-bash'), ('3', 'Sl+', 'claude', 'claude')]


def test_send_keys_to_bash_pane_rejected(monkeypatch):
    fake_pane(monkeypatch, BASH)
    sent = []
    monkeypatch.setattr(tmux, 'send_keys_literal', lambda *a, **k: sent.append(a))
    monkeypatch.setattr(tmux, 'send_keys', lambda *a, **k: sent.append(a))
    for literal in (True, False):
        with pytest.raises(Exception, match='not running a registered coding agent'):
            anyio.run(lambda: mcp_server.send_keys('agents:0', 'id', literal=literal))
    assert sent == []


def test_send_keys_to_agent_pane_goes_to_the_resolved_pane(monkeypatch):
    fake_pane(monkeypatch, CLAUDE, pane_id='%10')
    sent = []
    monkeypatch.setattr(tmux, 'send_keys_literal', lambda t, s, enter=True: sent.append((t, s, enter)))
    out = anyio.run(lambda: mcp_server.send_keys('agents:2', 'hello'))
    assert sent == [('%10', 'hello', True)] and out['agent'] == 'claude'


def test_dead_or_missing_pane_rejected(monkeypatch):
    fake_pane(monkeypatch, CLAUDE, dead='1')
    with pytest.raises(agents.NotAnAgentPane):
        agents.require_agent_pane('agents:2')

    def boom(t, f):
        raise tmux.TmuxError(['tmux'], 1, "can't find pane: nope")
    monkeypatch.setattr(tmux, 'display', boom)
    with pytest.raises(agents.NotAnAgentPane, match="can't find pane"):
        agents.require_agent_pane('nope')


def test_tell_agent_and_probe_refuse_bash(monkeypatch):
    fake_pane(monkeypatch, BASH)
    monkeypatch.setattr(tmux, 'send_keys_literal', lambda *a, **k: pytest.fail('typed into bash'))
    with pytest.raises(agents.NotAnAgentPane):
        agents.tell_agent('agents:0', 'hi', require_empty_prompt=False)
    with pytest.raises(agents.NotAnAgentPane):
        agents.pane_ready('agents:0', probe=True)
    assert agents.pane_ready('agents:0')['ready'] is None      # read-only check still answers


# ---------------------------------------------------------------------------
# Muse adapter (fixture captured from a live Muse pane)

def test_muse_placeholder_reads_as_blank():
    raw = (FIX / 'muse_idle.ansi').read_text()
    plain = agents.strip_ansi(raw)
    assert '❯ Ask to monitor' in plain
    assert agents.is_muse_screen(plain)
    assert not agents.muse_prompt_blank(plain)                 # the placeholder looks like input
    assert agents.muse_prompt_blank(agents.input_view(raw))    # until input_view() blanks the grey text


def test_muse_typed_text_is_not_blank():
    raw = (FIX / 'muse_idle.ansi').read_text().replace(
        '\x1b[38;5;242mAsk to monitor a running build or log and get pinged on changes',
        '\x1b[38;5;252mrun the tests')
    assert not agents.muse_prompt_blank(agents.input_view(raw))


def test_claude_dim_suggestion_reads_as_blank():
    raw = (FIX / 'claude_suggestion.ansi').read_text()
    assert not agents.claude_prompt_blank(agents.strip_ansi(raw))
    assert agents.claude_prompt_blank(agents.input_view(raw))
    typed = raw.replace('\x1b[2mcheck PR', 'check PR')
    assert not agents.claude_prompt_blank(agents.input_view(typed))


GHOST = "check PR #6 on github to confirm it's updated"
BAR = '\x1b[38;5;244m' + '─' * 60 + '\n\x1b[39m'
GREY_OUTPUT = '\x1b[2m  ⎿  Read 3 files (ctrl+o to expand)\x1b[0m\n'


def box(*body):
    """An escape-coded Claude screen: grey output above, then the ❯ input box and footer."""
    return ('● Done.\n' + GREY_OUTPUT + BAR + '❯\xa0' + '\n'.join(body) + '\n' + BAR +
            '  \x1b[38;5;246m? for shortcuts\x1b[39m\n')


def status_of(monkeypatch, raw, agent='claude'):
    """agents.agent_status() on a canned capture, without tmux."""
    monkeypatch.setattr(agents, 'pane_agent', lambda t: {'pane_id': '%10', 'agent': agent})
    monkeypatch.setattr(tmux, 'capture_pane', lambda t, escapes=False: raw)
    monkeypatch.setattr(tmux, 'list_panes', lambda t: [{'pane_id': '%10', 'current_command': agent}])
    return agents.agent_status('agents:2')


def test_agent_status_labels_the_fixture_suggestion(monkeypatch):
    raw = (FIX / 'claude_suggestion.ansi').read_text()
    st = status_of(monkeypatch, raw)
    assert st['prompt_blank'] is True
    assert st['prompt'] == {'input': '', 'suggestion': GHOST, 'blank': True}
    (line,) = [l for l in st['last_lines'] if '❯' in l]
    assert line.endswith(agents.SUGGESTION_LABEL.format(GHOST))
    assert 'not evidence of typed input' in st['screen_note']
    # the grey hint line above the box is unrelated output and stays as drawn
    assert any(l.strip() == 'new task? /clear to save 150.9k tokens' for l in st['last_lines'])
    assert st['screen_sha256'] == agents.screen_hash(agents.strip_ansi(raw))   # raw hash unchanged


def test_agent_status_identical_text_typed_is_input(monkeypatch):
    raw = (FIX / 'claude_suggestion.ansi').read_text().replace('\x1b[2mcheck PR', 'check PR')
    st = status_of(monkeypatch, raw)
    assert st['prompt_blank'] is False
    assert st['prompt'] == {'input': GHOST, 'suggestion': None, 'blank': False}
    assert not any('ghost suggestion' in l for l in st['last_lines'])
    assert any(l.endswith('❯\xa0' + GHOST) for l in st['last_lines'])


def test_agent_status_typed_prefix_plus_suggestion(monkeypatch):
    st = status_of(monkeypatch, box('merge\x1b[2m it into work\x1b[0m'))
    assert st['prompt_blank'] is False
    assert st['prompt'] == {'input': 'merge', 'suggestion': 'it into work', 'blank': False}
    assert '❯\xa0merge [ghost suggestion, not typed: it into work]' in st['last_lines']


@pytest.mark.parametrize('body, typed', [
    (['', '  second line typed'], '\nsecond line typed'),          # blank ❯ line, real continuation
    (['first', '  second'], 'first\nsecond'),
    (['first', '\x1b[2m  ghost on line two\x1b[0m'], 'first'),
])
def test_multiline_continuation_counts(monkeypatch, body, typed):
    raw = box(*body)
    assert agents.claude_prompt_blank(agents.input_view(raw)) is False
    st = status_of(monkeypatch, raw)
    assert st['prompt_blank'] is False and st['prompt']['input'] == typed


def test_blank_prompt_with_empty_continuation_is_blank(monkeypatch):
    raw = box('', '   ')
    assert agents.claude_prompt_blank(agents.input_view(raw))
    assert status_of(monkeypatch, raw)['prompt'] == {'input': '', 'suggestion': None, 'blank': True}


@pytest.mark.parametrize('body, typed', [
    ('\x1b[2m\x1b[0mreal text', 'real text'),                    # reset before the text
    ('\x1b[2m\x1b[22mreal text', 'real text'),                   # dim turned off with 22
    ('\x1b[38;5;244m\x1b[39mreal text', 'real text'),            # grey turned off with 39
    ('\x1b[2mgrey\x1b[0m then typed', 'grey then typed'),         # typed text after a hint: all input
])
def test_style_resets_end_the_suggestion(monkeypatch, body, typed):
    st = status_of(monkeypatch, box(body))
    assert st['prompt'] == {'input': typed, 'suggestion': None, 'blank': False}
    assert st['prompt_blank'] is False


@pytest.mark.parametrize('sgr', ['3', '1', '4', '34', '38;5;153', '38;2;120;120;120', '48;5;240', '7'])
def test_unfamiliar_styles_are_kept_as_input(monkeypatch, sgr):
    st = status_of(monkeypatch, box(f'\x1b[{sgr}mmerge it into work\x1b[0m'))
    assert st['prompt'] == {'input': 'merge it into work', 'suggestion': None, 'blank': False}
    assert st['prompt_blank'] is False


def test_unrelated_dim_output_is_not_relabelled_or_erased(monkeypatch):
    st = status_of(monkeypatch, box('\x1b[2mmerge it into work\x1b[0m'))
    assert st['prompt']['suggestion'] == 'merge it into work'
    assert '  ⎿  Read 3 files (ctrl+o to expand)' in st['last_lines']
    assert '  ? for shortcuts' in st['last_lines']
    assert sum('ghost suggestion' in l for l in st['last_lines']) == 1


def test_other_agents_get_plain_last_lines(monkeypatch):
    raw = '\x1b[2mdim output\x1b[0m\n› \x1b[2mghost\x1b[0m\n'
    st = status_of(monkeypatch, raw, agent='codex')
    assert st['prompt'] is None and st['last_lines'] == ['dim output', '› ghost']
    assert st['prompt_blank'] is True


def test_wait_for_idle_timeout_labels_without_erasing(monkeypatch):
    t = [0.0]
    res = agents.wait_for_idle('t', timeout=2, poll=1, settle=3, agent='claude',
                               capture=lambda: box('typed\x1b[2m and ghost\x1b[0m'),
                               sleep=lambda s: t.__setitem__(0, t[0] + s), clock=lambda: t[0])
    assert not res['idle'] and res['prompt_blank'] is False
    assert '  ⎿  Read 3 files (ctrl+o to expand)' in res['last_lines']
    assert '❯\xa0typed [ghost suggestion, not typed: and ghost]' in res['last_lines']


def test_tell_agent_still_refuses_typed_multiline_input(monkeypatch):
    fake_pane(monkeypatch, CLAUDE, pane_id='%10')
    raw = box('', '  half-typed follow-up')
    monkeypatch.setattr(tmux, 'capture_pane', lambda t, escapes=False: raw)
    monkeypatch.setattr(tmux, 'send_keys_literal', lambda *a, **k: None)
    monkeypatch.setattr(tmux, 'send_keys', lambda *a, **k: subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(agents.time, 'sleep', lambda s: None)
    with pytest.raises(agents.AgentNotReady):
        agents.tell_agent('agents:2', 'hello')


def test_capture_descriptions_warn_against_reading_input():
    assert 'not evidence of typed input' in mcp_server.capture_pane.__doc__
    assert 'ghost suggestion, not typed' in mcp_server.agent_status.__doc__
    assert 'not evidence of typed input' in mcp_server.INSTRUCTIONS


def test_muse_tui_wiring():
    tui = agents.tui_for('muse', '%8', capture=lambda t: agents.input_view((FIX / 'muse_idle.ansi').read_text()))
    assert isinstance(tui, agents.MuseTui) and tui.is_prompt_blank()
    assert agents.detector_for('muse') is agents.muse_prompt_blank


def test_registry_info_lists_all_five(fake_bins):
    info = {a['name']: a for a in registry.registry_info()}
    assert set(info) == {'claude', 'muse', 'grok', 'gemini', 'codex'}
    assert info['muse']['installed'] and not info['grok']['installed']
