"""swarm-lock: one global non-blocking gate lock; a second caller exits at once with busy."""
import subprocess
import sys
import time

import pytest

from tangentswarm import gatelock as gl, tick


@pytest.fixture
def lock(tmp_path, monkeypatch):
    p = tmp_path / 'gate.lock'
    monkeypatch.setattr(gl, 'LOCK', p)
    monkeypatch.delenv(gl.HELD_ENV, raising=False)
    return p


HOLDER = (
    "import sys, time\n"
    "from pathlib import Path\n"
    "from tangentswarm import gatelock as gl\n"
    "with gl.gate_lock('hr-gate --apply 469', lock=Path(sys.argv[1]), env={}):\n"
    "    print('ready', flush=True)\n"
    "    time.sleep(float(sys.argv[2]))\n")


def holder_proc(path, seconds):
    p = subprocess.Popen([sys.executable, '-c', HOLDER, str(path), str(seconds)], stdout=subprocess.PIPE, text=True)
    assert p.stdout.readline().strip() == 'ready'
    return p


def test_second_caller_is_busy_at_once_and_names_the_holder(lock):
    p = holder_proc(lock, 3)
    try:
        t0 = time.time()
        with pytest.raises(gl.Busy) as e:
            with gl.gate_lock('pending-promote --apply', env={}):
                pass
        assert time.time() - t0 < 0.5
        assert 'hr-gate --apply 469' in str(e.value)
        assert gl.is_busy()
    finally:
        p.wait()
    assert not gl.is_busy()
    with gl.gate_lock('pending-promote --apply', env={}):
        assert 'pending-promote' in gl.holder()


def test_held_env_means_a_parent_holds_it(lock):
    with gl.gate_lock('parent', env={}):
        with gl.gate_lock('child', env={gl.HELD_ENV: '1'}):
            pass


def test_cli_runs_command_under_lock_and_passes_held_env(lock, capsys):
    rc = gl.main(['-n', 'work-merge', '--', sys.executable, '-c',
                  f"import os,sys; sys.exit(0 if os.environ.get('{gl.HELD_ENV}') == '1' else 9)"])
    assert rc == 0


def test_cli_busy_exits_75(lock, capsys):
    p = holder_proc(lock, 2)
    try:
        assert gl.main(['--', 'true']) == gl.EXIT_BUSY
        assert gl.main(['--status']) == gl.EXIT_BUSY
        assert capsys.readouterr().out.startswith('busy: hr-gate --apply 469')
    finally:
        p.wait()
    assert gl.main(['--status']) == 0


def test_cli_needs_a_command(lock):
    with pytest.raises(SystemExit):
        gl.main([])


# --- swarm-tick treats busy as "try next pass" ---------------------------------

def bare_tick():
    t = tick.Tick.__new__(tick.Tick)
    t.dry, t.verbose, t.out, t.did, t.notes = False, False, print, [], []
    return t


def test_tick_pending_busy_is_a_note_not_an_error():
    t = bare_tick()
    t.tool = lambda argv: (gl.EXIT_BUSY, 'busy: hr-gate --apply 469 pid 1 since 17:20\n')
    t.pending_pass()
    assert t.did == [] and len(t.notes) == 1
    assert 'pending-promote skipped, gate lock busy: hr-gate --apply 469' in t.notes[0][1]
    assert 'error' not in t.notes[0][1]
