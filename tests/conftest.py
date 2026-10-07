import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Keep logs, auth db and config out of the real home directory."""
    monkeypatch.setenv('TANGENTSWARM_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.setenv('TANGENTSWARM_CONFIG_DIR', str(tmp_path / 'config'))
    for var in list(os.environ):
        if var.startswith('TANGENTSWARM_AUTH_'):
            monkeypatch.delenv(var, raising=False)
    yield tmp_path
