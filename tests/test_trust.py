"""Pre-trusting worktrees (trust.py) and naming the first-run screens that block an agent.
Every config lives under the temp HOME that conftest.py sets, never the real one."""
import json
import os
import stat
import subprocess
import threading
import time
from pathlib import Path

import pytest

tomllib = pytest.importorskip('tomllib')

from tangentswarm import agents, registry, tmux, trust, worktrees

FIX = Path(__file__).parent / 'fixtures'

CLAUDE_JSON = {
    'numStartups': 7,
    'theme': 'dark',
    'oauthAccount': {'emailAddress': 'someone@example.com'},
    'projects': {
        '/elsewhere': {'allowedTools': ['Bash(ls)'], 'hasTrustDialogAccepted': True},
        '/untrusted': {'hasTrustDialogAccepted': False},
    },
}


@pytest.fixture
def home(tmp_path):
    h = Path(os.environ['HOME'])
    assert str(h).startswith(str(tmp_path))          # conftest's temp HOME, never the real one
    return h


@pytest.fixture
def claude_json(home):
    p = home / '.claude.json'
    p.write_text(json.dumps(CLAUDE_JSON, indent=2))   # Claude writes no trailing newline
    p.chmod(0o600)
    return p


@pytest.fixture
def wt(tmp_path):
    d = tmp_path / 'ver' / 'proj.feat'
    d.mkdir(parents=True)
    return d


def no_temp_files(d):
    return not [n for n in os.listdir(d) if n.endswith('.tmp') or n.endswith('.lock')]


# ---------------------------------------------------------------------------
# Claude Code: ~/.claude.json

def test_claude_config_is_under_the_temp_home(home, monkeypatch):
    assert trust.claude_config_path() == str(home / '.claude.json')
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(home / 'cfg'))
    assert trust.claude_config_path() == str(home / 'cfg' / '.claude.json')


def test_claude_trust_sets_only_that_key(claude_json, wt):
    out = trust.trust_claude(str(wt))
    assert out['status'] == 'trusted' and out['key'] == str(wt.resolve())
    data = json.loads(claude_json.read_text())
    assert data['projects'].pop(str(wt.resolve())) == {'hasTrustDialogAccepted': True}
    assert data == CLAUDE_JSON
    assert stat.S_IMODE(claude_json.stat().st_mode) == 0o600
    assert not claude_json.read_text().endswith('\n')
    assert claude_json.read_text().startswith('{\n  "numStartups": 7,')   # same 2-space layout
    assert no_temp_files(claude_json.parent)


def test_claude_trust_keeps_the_file_mode_and_existing_entry(claude_json, wt):
    claude_json.chmod(0o640)
    data = dict(CLAUDE_JSON, projects={str(wt): {'allowedTools': ['Read'], 'hasTrustDialogAccepted': False}})
    claude_json.write_text(json.dumps(data, indent=2) + '\n')
    assert trust.trust_claude(str(wt))['status'] == 'trusted'
    assert stat.S_IMODE(claude_json.stat().st_mode) == 0o640
    assert claude_json.read_text().endswith('}\n')
    assert json.loads(claude_json.read_text())['projects'][str(wt)] == \
        {'allowedTools': ['Read'], 'hasTrustDialogAccepted': True}


def test_claude_trust_is_idempotent(claude_json, wt):
    assert trust.trust_claude(str(wt))['status'] == 'trusted'
    before = claude_json.read_bytes(), claude_json.stat().st_ino, claude_json.stat().st_mtime_ns
    assert trust.trust_claude(str(wt))['status'] == 'already_trusted'
    assert (claude_json.read_bytes(), claude_json.stat().st_ino, claude_json.stat().st_mtime_ns) == before


def test_claude_trust_uses_the_real_path(claude_json, wt, tmp_path):
    link = tmp_path / 'link'
    link.symlink_to(wt)
    assert trust.trust_claude(str(link))['key'] == str(wt.resolve())


def test_claude_config_symlink_is_kept(home, wt):
    real = home / 'dotfiles' / 'claude.json'
    real.parent.mkdir()
    real.write_text('{}')
    (home / '.claude.json').symlink_to(real)
    assert trust.trust_claude(str(wt))['status'] == 'trusted'
    assert (home / '.claude.json').is_symlink()
    assert json.loads(real.read_text()) == {'projects': {str(wt): {'hasTrustDialogAccepted': True}}}


def test_claude_missing_config_is_not_created(home, wt):
    out = trust.trust_claude(str(wt))
    assert out['status'] == 'skipped' and 'does not exist' in out['warning']
    assert not (home / '.claude.json').exists() and no_temp_files(home)


@pytest.mark.parametrize('text', ['', '{"projects": {', 'null', '[]', '{"projects": []}',
                                  '{"projects": {"%s": true}}'])
def test_claude_malformed_config_is_never_clobbered(home, wt, text):
    p = home / '.claude.json'
    p.write_text(text.replace('%s', str(wt)))
    before = p.read_bytes()
    out = trust.trust_claude(str(wt))
    assert out['status'] == 'skipped' and out['warning']
    assert p.read_bytes() == before and no_temp_files(home)


def test_claude_waits_for_a_held_lock_then_gives_up(claude_json, wt, monkeypatch):
    lock = Path(str(claude_json) + '.lock')
    lock.mkdir()                                     # Claude Code holds it (fresh mtime)
    monkeypatch.setattr(trust, 'LOCK_TIMEOUT', 0.3)
    before = claude_json.read_bytes()
    out = trust.trust_claude(str(wt))
    assert out['status'] == 'skipped' and 'timed out' in out['warning']
    assert claude_json.read_bytes() == before and lock.is_dir()     # not ours to remove


def test_claude_takes_over_a_stale_lock(claude_json, wt):
    lock = Path(str(claude_json) + '.lock')
    lock.mkdir()
    old = time.time() - trust.LOCK_STALE - 5
    os.utime(lock, (old, old))
    assert trust.trust_claude(str(wt))['status'] == 'trusted'
    assert not lock.exists()


def test_claude_lock_is_released_after_a_held_lock_clears(claude_json, wt):
    lock = Path(str(claude_json) + '.lock')
    lock.mkdir()
    threading.Timer(0.3, lock.rmdir).start()
    assert trust.trust_claude(str(wt))['status'] == 'trusted'


def test_claude_lock_sits_beside_a_symlinked_config(home, wt, monkeypatch):
    """proper-lockfile resolves symlinks, so Claude locks beside the target; so must we."""
    (home / 'dotfiles').mkdir()
    target = home / 'dotfiles' / 'claude.json'
    target.write_text(json.dumps(CLAUDE_JSON))
    (home / '.claude.json').symlink_to(target)
    lock = Path(str(target) + '.lock')
    lock.mkdir()                                     # Claude Code holds the target's lock
    monkeypatch.setattr(trust, 'LOCK_TIMEOUT', 0.3)
    out = trust.trust_claude(str(wt))
    assert out['status'] == 'skipped' and 'timed out' in out['warning']
    lock.rmdir()
    assert trust.trust_claude(str(wt))['status'] == 'trusted'
    assert (home / '.claude.json').is_symlink() and not Path(str(home / '.claude.json') + '.lock').exists()
    assert json.loads(target.read_text())['projects'][str(wt.resolve())] == {'hasTrustDialogAccepted': True}


def test_claude_concurrent_writers_lose_nothing(claude_json, tmp_path):
    """Many tangentswarm launches plus a Claude-like writer (read, change its own key,
    write under the same lock) racing on one file."""
    dirs = []
    for i in range(16):
        d = tmp_path / 'ver' / f'proj.b{i}'
        d.mkdir(parents=True)
        dirs.append(d)

    def claude_writer():
        for n in range(20):
            with trust.config_lock(str(claude_json)):
                data = json.loads(claude_json.read_text())
                data['numStartups'] += 1
                tmp = claude_json.with_name('.c.tmp')
                tmp.write_text(json.dumps(data, indent=2))
                os.replace(tmp, claude_json)

    results = []
    threads = [threading.Thread(target=lambda d=d: results.append(trust.trust_claude(str(d))))
               for d in dirs] + [threading.Thread(target=claude_writer)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(r['status'] for r in results) == ['trusted'] * len(dirs)
    data = json.loads(claude_json.read_text())
    assert data['numStartups'] == 7 + 20
    assert all(data['projects'][str(d)] == {'hasTrustDialogAccepted': True} for d in dirs)
    assert data['projects']['/elsewhere'] == CLAUDE_JSON['projects']['/elsewhere']
    assert no_temp_files(claude_json.parent)


def test_claude_refuses_to_replace_a_file_that_changed_underneath(claude_json, wt, monkeypatch):
    """A writer that ignores the lock: our replace notices and leaves its write alone."""
    real_mkstemp = trust.tempfile.mkstemp

    def racing_mkstemp(**kw):
        claude_json.write_text(json.dumps({'projects': {}, 'other': 'writer'}))
        return real_mkstemp(**kw)
    monkeypatch.setattr(trust.tempfile, 'mkstemp', racing_mkstemp)
    out = trust.trust_claude(str(wt))
    assert out['status'] == 'skipped' and 'changed' in out['warning']
    assert json.loads(claude_json.read_text()) == {'projects': {}, 'other': 'writer'}
    assert no_temp_files(claude_json.parent)


# ---------------------------------------------------------------------------
# Codex: ~/.codex/config.toml

CODEX_TOML = '''model = "gpt-6"

[tui]
screen_reader_detection_done = true

[projects."/elsewhere"]
trust_level = "trusted"
'''


@pytest.fixture
def codex_toml(home):
    p = home / '.codex' / 'config.toml'
    p.parent.mkdir()
    p.write_text(CODEX_TOML)
    p.chmod(0o600)
    return p


def test_codex_trust_appends_one_table(codex_toml, wt):
    out = trust.trust_codex(str(wt))
    assert out['status'] == 'trusted' and out['config'] == str(codex_toml)
    text = codex_toml.read_text()
    assert text.startswith(CODEX_TOML)               # everything that was there is kept verbatim
    data = tomllib.loads(text)
    assert data['projects'][str(wt)] == {'trust_level': 'trusted'}
    assert data['projects']['/elsewhere'] == {'trust_level': 'trusted'} and data['model'] == 'gpt-6'
    assert stat.S_IMODE(codex_toml.stat().st_mode) == 0o600
    assert trust.trust_codex(str(wt))['status'] == 'already_trusted'
    assert codex_toml.read_text() == text and no_temp_files(codex_toml.parent)


def test_codex_home_is_honoured(home, wt, monkeypatch):
    monkeypatch.setenv('CODEX_HOME', str(home / 'cx'))
    (home / 'cx').mkdir()
    assert trust.trust_codex(str(wt))['status'] == 'trusted'
    p = home / 'cx' / 'config.toml'
    assert tomllib.loads(p.read_text()) == {'projects': {str(wt): {'trust_level': 'trusted'}}}
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_codex_without_a_codex_dir_is_skipped(home, wt):
    out = trust.trust_codex(str(wt))
    assert out['status'] == 'skipped' and not (home / '.codex').exists()


def test_codex_explicit_untrusted_is_respected(codex_toml, wt):
    codex_toml.write_text(CODEX_TOML + f'\n[projects."{wt}"]\ntrust_level = "untrusted"\n')
    before = codex_toml.read_bytes()
    out = trust.trust_codex(str(wt))
    assert out['status'] == 'skipped' and "'untrusted'" in out['warning']
    assert codex_toml.read_bytes() == before


@pytest.mark.parametrize('text', ['model = "x"\n[tui\n', 'projects = { "/a" = { trust_level = "trusted" } }\n'])
def test_codex_unparsable_or_unappendable_config_is_left_alone(codex_toml, wt, text):
    codex_toml.write_text(text)
    out = trust.trust_codex(str(wt))
    assert out['status'] == 'skipped' and out['warning']
    assert codex_toml.read_text() == text and no_temp_files(codex_toml.parent)


def test_codex_quotes_odd_paths(codex_toml, tmp_path):
    d = tmp_path / 'ver' / 'we"ird\\name'
    d.mkdir(parents=True)
    assert trust.trust_codex(str(d))['status'] == 'trusted'
    assert tomllib.loads(codex_toml.read_text())['projects'][str(d)] == {'trust_level': 'trusted'}


def test_pretrust_by_agent(claude_json, codex_toml, wt):
    assert trust.pretrust('claude', str(wt))['status'] == 'trusted'
    assert trust.pretrust('codex', str(wt))['status'] == 'trusted'
    assert trust.pretrust('muse', str(wt))['flag'] == '--trust-workspace'
    assert trust.pretrust('gemini', str(wt))['status'] == 'unsupported'


# ---------------------------------------------------------------------------
# launching pre-trusts the worktree

class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, argv):
        self.calls.append(argv)
        return subprocess.CompletedProcess(
            argv, 0, stdout='agents\t5\tclaude-feat\t0\t%99\t1\t0\tclaude\t80\t24\t1\t0\t/x\n', stderr='')


@pytest.fixture
def claude_bin(tmp_path, monkeypatch):
    p = tmp_path / 'bin' / 'claude'
    p.parent.mkdir()
    p.write_text('#!/bin/sh\n')
    p.chmod(0o755)
    monkeypatch.setenv('TANGENTSWARM_AGENT_CLAUDE', str(p))
    monkeypatch.setattr(tmux, 'has_session', lambda s: True)
    return str(p)


def test_launch_trusts_before_starting(claude_json, wt, claude_bin, monkeypatch):
    monkeypatch.setenv('TANGENTSWARM_AGENT_ROOT', str(wt.parent))
    seen = []

    def run(argv):
        seen.append(json.loads(claude_json.read_text())['projects'].get(str(wt)))
        return Recorder()(argv)
    out = registry.launch_agent('claude', str(wt), run=run)
    assert seen == [{'hasTrustDialogAccepted': True}]        # already trusted when tmux ran
    assert out['trust']['status'] == 'trusted'


def test_launch_goes_ahead_with_a_warning_when_trust_fails(home, wt, claude_bin, monkeypatch):
    monkeypatch.setenv('TANGENTSWARM_AGENT_ROOT', str(wt.parent))
    rec = Recorder()
    out = registry.launch_agent('claude', str(wt), run=rec)
    assert rec.calls and out['trust']['status'] == 'skipped' and out['trust']['warning']


@pytest.fixture
def top(tmp_path, monkeypatch):
    """An agent root holding a plain clone 'proj' of a throwaway origin."""
    for k, v in {'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@example.com',
                 'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@example.com',
                 'GIT_CONFIG_GLOBAL': str(tmp_path / 'gitconfig'), 'GIT_CONFIG_NOSYSTEM': '1'}.items():
        monkeypatch.setenv(k, v)
    origin, top, seed = tmp_path / 'origin.git', tmp_path / 'top', tmp_path / 'seed'
    seed.mkdir()
    top.mkdir()
    for args, cwd in [(['git', 'init', '-q', '-b', 'main'], seed),
                      (['git', 'commit', '-q', '--allow-empty', '-m', 'init'], seed),
                      (['git', 'clone', '-q', '--bare', str(seed), str(origin)], tmp_path),
                      (['git', 'clone', '-q', str(origin), 'proj'], top)]:
        subprocess.run(args, cwd=cwd, check=True, capture_output=True)
    monkeypatch.setenv('TANGENTSWARM_AGENT_ROOT', str(top))
    monkeypatch.setattr(worktrees, '_all_panes', lambda: [])
    return top


def test_spawn_trusts_the_worktree_path_and_reports_startup(claude_json, claude_bin, top, monkeypatch):
    watched = []
    monkeypatch.setattr(worktrees, '_watch_startup', lambda pane, agent, wait=None:
                        watched.append((pane, agent)) or {'state': 'ready', 'blocked': None})
    out = worktrees.spawn_agent('claude', 'proj', 'feat', run=Recorder())
    path = str((top / 'proj.feat').resolve())
    assert out['trust']['key'] == path
    assert json.loads(claude_json.read_text())['projects'][path] == {'hasTrustDialogAccepted': True}
    assert str((top / 'proj').resolve()) not in json.loads(claude_json.read_text())['projects']
    assert watched == [('%99', 'claude')] and out['startup']['state'] == 'ready'


def test_spawn_warns_when_the_trust_entry_was_removed(claude_json, claude_bin, top, monkeypatch):
    before = claude_json.read_text()

    def clobbered_then_blocked(pane, agent, wait=None):
        claude_json.write_text(before)                 # an exiting Claude writes its old copy back
        return {'state': 'blocked', 'blocked': {'kind': 'trust_prompt'}}
    monkeypatch.setattr(worktrees, '_watch_startup', clobbered_then_blocked)
    out = worktrees.spawn_agent('claude', 'proj', 'feat', run=Recorder())
    assert out['trust']['status'] == 'trusted' and 'no longer in' in out['trust']['warning']
    assert trust.is_trusted('claude', str(top / 'proj.feat')) is False
    assert trust.is_trusted('gemini', str(top / 'proj.feat')) is None


def test_spawn_warns_when_a_trust_flag_did_not_work(top, monkeypatch):
    monkeypatch.setattr(registry, 'launch_agent', lambda agent, path, **kw: {
        'agent': agent, 'pane': {'pane_id': '%7'},
        'trust': {'agent': agent, 'method': 'flag', 'flag': '--trust-workspace', 'status': 'trusted'}})
    monkeypatch.setattr(registry, 'launch_argv', lambda agent: ['muse'])
    monkeypatch.setattr(worktrees, '_watch_startup', lambda pane, agent, wait=None:
                        {'state': 'blocked', 'blocked': {'kind': 'trust_prompt'}})
    out = worktrees.spawn_agent('muse', 'proj', 'feat', run=Recorder())
    assert out['startup']['state'] == 'blocked' and '--trust-workspace' in out['trust']['warning']


def test_spawn_reports_trust_when_reusing_an_agent(claude_json, claude_bin, top, monkeypatch):
    worktrees.spawn_agent('claude', 'proj', 'feat', run=Recorder())
    live = [{'pane_id': '%5', 'target': 'agents:2', 'window_name': 'claude-feat', 'agent': 'claude'}]
    monkeypatch.setattr(worktrees, '_live_agents', lambda path: live)
    out = worktrees.spawn_agent('claude', 'proj', 'feat', run=Recorder())
    assert out['reused'] is True
    assert out['trust']['status'] == 'reused' and out['trust']['trusted'] is True
    assert out['trust']['key'] == str((top / 'proj.feat').resolve())


def test_spawn_warns_when_the_agent_reads_another_config(claude_json, claude_bin, top, monkeypatch):
    monkeypatch.setattr(worktrees, '_watch_startup', lambda pane, agent, wait=None:
                        {'state': 'blocked', 'blocked': {'kind': 'trust_prompt'}})
    out = worktrees.spawn_agent('claude', 'proj', 'feat', run=Recorder())
    assert out['trust']['status'] == 'trusted' and "tmux server's HOME" in out['trust']['warning']


# ---------------------------------------------------------------------------
# naming blocking screens (fixtures captured from Claude Code 2.1.302 and Codex 0.157.0)

BLOCKED = [
    ('claude_trust.ansi', 'claude', 'trust_prompt'),
    ('claude_theme.ansi', 'claude', 'theme_picker'),
    ('claude_login.ansi', 'claude', 'login'),
    ('claude_apikey.ansi', 'claude', 'api_key_prompt'),
    ('claude_bypass.ansi', 'claude', 'bypass_permissions_confirm'),
    ('codex_trust.ansi', 'codex', 'trust_prompt'),
    ('codex_login.ansi', 'codex', 'login'),
    ('codex_update.ansi', 'codex', 'update_prompt'),
]
NOT_BLOCKED = [('claude_ready.ansi', 'claude'), ('claude_suggestion.ansi', 'claude'),
               ('claude_placeholder.ansi', 'claude'),
               ('codex_ready.ansi', 'codex'), ('muse_idle.ansi', 'muse')]


@pytest.mark.parametrize('name,agent,kind', BLOCKED)
def test_blocking_screens_are_named(name, agent, kind):
    raw = (FIX / name).read_text()
    for screen in (agents.strip_ansi(raw), agents.input_view(raw)):
        b = agents.blocking_screen(screen, agent)
        assert b and b['kind'] == kind and b['lines'] and b['hint']
    assert not agents.detector_for(agent)(agents.input_view(raw))     # never reads as a blank prompt


@pytest.mark.parametrize('name,agent', NOT_BLOCKED)
def test_working_screens_are_not_blocked(name, agent):
    raw = (FIX / name).read_text()
    assert agents.blocking_screen(agents.strip_ansi(raw), agent) is None
    assert agents.detector_for(agent)(agents.input_view(raw))


def test_grey_angle_brackets_in_a_placeholder_are_not_input():
    """Claude's 'Try "refactor <filepath>"' suggestion is dim; its '>' is not Gemini's prompt."""
    raw = (FIX / 'claude_placeholder.ansi').read_text()
    assert 'refactor <filepath>' in agents.strip_ansi(raw)
    assert agents.claude_prompt_blank(agents.input_view(raw))
    assert agents.input_view('\x1b[2m> \x1b[0m\n  \x1b[2mTry <x>\x1b[0m') == '> \n         '


def test_a_transcript_quoting_a_dialog_is_not_blocked():
    trust_text = agents.strip_ansi((FIX / 'claude_trust.ansi').read_text())
    ready = agents.strip_ansi((FIX / 'claude_ready.ansi').read_text())
    assert agents.blocking_screen(trust_text + '\n' + ready, 'claude') is None


def test_a_codex_transcript_quoting_a_dialog_is_not_blocked():
    ready = agents.strip_ansi((FIX / 'codex_ready.ansi').read_text()).split('\n')
    at = next(i for i, l in enumerate(ready) if l.lstrip().startswith('›'))
    quoted = ready[:at] + ['• The README says: Trust this folder? Pick "Trust and continue".',
                           '  Do you trust this folder?'] + ready[at:]
    assert agents.blocking_screen('\n'.join(quoted), 'codex') is None
    trust_screen = agents.strip_ansi((FIX / 'codex_trust.ansi').read_text())
    moved = trust_screen.replace('› 1. Trust', '  1. Trust').replace('  2. Quit', '› 2. Quit')
    assert agents.blocking_screen(moved, 'codex')['kind'] == 'trust_prompt'


def test_generic_trust_phrases_need_a_live_menu():
    """Grok has no input-box check, so a quoted question alone must not block it."""
    dialog = 'Do you trust this folder?\n> 1. Yes\n  2. No'
    assert agents.blocking_screen(dialog, 'grok')['kind'] == 'trust_prompt'
    assert agents.blocking_screen(dialog, None)['kind'] == 'trust_prompt'
    chat = 'user: what does "Do you trust this folder?" mean?\ngrok: It is the first-run dialog.\n> '
    assert agents.blocking_screen(chat, 'grok') is None
    scrolled = dialog + '\n' + '\n'.join(f'line {i}' for i in range(20)) + '\n> '
    assert agents.blocking_screen(scrolled, 'grok') is None


def test_kinds_for_other_agents_do_not_cross_over():
    codex_update = agents.strip_ansi((FIX / 'codex_update.ansi').read_text())
    assert agents.blocking_screen(codex_update, 'claude') is None
    generic = 'Do you trust this folder?\n 1. Trust folder\n 2. Don\'t trust'
    assert agents.blocking_screen(generic, 'gemini')['kind'] == 'trust_prompt'


def screens(*names):
    return [(FIX / n).read_text() if n.endswith('.ansi') else n for n in names]


def test_watch_startup_reports_the_first_blocking_screen():
    seq = iter(screens('loading...', 'loading...', 'claude_trust.ansi'))
    out = agents.watch_startup('%9', 'claude', capture=lambda p: next(seq), sleep=lambda s: None)
    assert out['state'] == 'blocked' and out['blocked']['kind'] == 'trust_prompt'


def test_watch_startup_ready_starting_and_exited():
    seq = iter(screens('loading...', 'claude_ready.ansi'))
    assert agents.watch_startup('%9', 'claude', capture=lambda p: next(seq),
                                sleep=lambda s: None)['state'] == 'ready'
    t = iter(range(100))
    out = agents.watch_startup('%9', 'claude', wait=3, capture=lambda p: 'loading...',
                               sleep=lambda s: None, clock=lambda: next(t))
    assert out['state'] == 'starting' and out['blocked'] is None

    def gone(p):
        raise tmux.TmuxError(['tmux'], 1, "can't find pane")
    assert agents.watch_startup('%9', 'claude', capture=gone)['state'] == 'exited'


# ---------------------------------------------------------------------------
# agent_status, pane_ready and tell_agent on a blocked pane

CLAUDE = [('1', 'Ss', 'bash', '-bash'), ('3', 'Sl+', 'claude', 'claude')]


@pytest.fixture
def blocked_pane(monkeypatch):
    monkeypatch.setattr(tmux, 'display', lambda t, f: '%11\t/dev/pts/8\t0\tagents\t0\t0')
    monkeypatch.setattr(agents, 'foreground_processes', lambda tty: CLAUDE)
    monkeypatch.setattr(tmux, 'capture_pane', lambda t, escapes=False: (FIX / 'claude_trust.ansi').read_text())
    monkeypatch.setattr(tmux, 'list_panes', lambda *a, **k: [{'pane_id': '%11', 'current_command': 'claude',
                                                             'current_path': '/x'}])
    typed = []
    monkeypatch.setattr(tmux, 'send_keys_literal', lambda *a, **k: typed.append(a))
    monkeypatch.setattr(tmux, 'send_keys', lambda *a, **k: typed.append(a))
    return typed


def test_agent_status_names_the_screen(blocked_pane):
    st = agents.agent_status('agents:0')
    assert st['blocked']['kind'] == 'trust_prompt' and st['prompt_blank'] is False


def test_pane_ready_reports_blocked_without_probing(blocked_pane):
    for probe in (False, True):
        r = agents.pane_ready('agents:0', probe=probe)
        assert r['ready'] is False and r['blocked']['kind'] == 'trust_prompt'
    assert blocked_pane == []


@pytest.mark.parametrize('require_empty_prompt', [True, False])
def test_tell_agent_refuses_on_a_blocking_screen(blocked_pane, require_empty_prompt):
    with pytest.raises(agents.AgentBlocked, match='on the trust_prompt screen') as e:
        agents.tell_agent('agents:0', 'hi', require_empty_prompt=require_empty_prompt)
    assert e.value.blocked['kind'] == 'trust_prompt'
    assert blocked_pane == []                       # not even the space probe


def test_wait_for_idle_returns_on_a_blocking_screen():
    raw = agents.input_view((FIX / 'codex_update.ansi').read_text())
    t = iter(range(100))
    out = agents.wait_for_idle('%1', agent='codex', settle=2, capture=lambda: raw,
                               sleep=lambda s: None, clock=lambda: next(t))
    assert out['idle'] is False and out['blocked']['kind'] == 'update_prompt'
