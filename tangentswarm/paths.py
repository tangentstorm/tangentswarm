"""Filesystem locations used by tangentswarm (all outside the repo)."""
import os
from pathlib import Path


def _xdg(var, default):
    val = os.environ.get(var)
    return Path(val) if val else Path.home() / default


def state_dir():
    """~/.local/state/tangentswarm (or $TANGENTSWARM_STATE_DIR)."""
    override = os.environ.get('TANGENTSWARM_STATE_DIR')
    return Path(override) if override else _xdg('XDG_STATE_HOME', '.local/state') / 'tangentswarm'


def config_dir():
    """~/.config/tangentswarm (or $TANGENTSWARM_CONFIG_DIR)."""
    override = os.environ.get('TANGENTSWARM_CONFIG_DIR')
    return Path(override) if override else _xdg('XDG_CONFIG_HOME', '.config') / 'tangentswarm'


def data_dir():
    """~/.local/share/tangentswarm."""
    return _xdg('XDG_DATA_HOME', '.local/share') / 'tangentswarm'


def ensure_private_dir(path):
    """Create path (and parents) and make it 0700."""
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def write_private_file(path, data):
    """Write text to path with mode 0600 (created that way, never world-readable)."""
    path = Path(path)
    ensure_private_dir(path.parent)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        f.write(data)
    os.chmod(path, 0o600)
    return path
