import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Keep logs, auth db and config out of the real home directory. HOME points at an
    empty temp dir, so a launch in a test can never pre-trust anything in the real
    ~/.claude.json or ~/.codex/config.toml, and nothing watches a real tmux pane."""
    monkeypatch.setenv('TANGENTSWARM_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.setenv('TANGENTSWARM_CONFIG_DIR', str(tmp_path / 'config'))
    (tmp_path / 'home').mkdir(exist_ok=True)
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    for var in ('CLAUDE_CONFIG_DIR', 'CODEX_HOME'):
        monkeypatch.delenv(var, raising=False)
    from tangentswarm import worktrees
    monkeypatch.setattr(worktrees, '_watch_startup',
                        lambda pane, agent, wait=None: {'state': 'unwatched', 'blocked': None})
    for var in list(os.environ):
        if var.startswith(('TANGENTSWARM_AUTH_', 'TANGENTSWARM_API_KEY', 'TANGENTSWARM_AGENT_')):
            monkeypatch.delenv(var, raising=False)
    yield tmp_path
