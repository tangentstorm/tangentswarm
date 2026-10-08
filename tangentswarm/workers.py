"""Swarm control-directory config: workers.jsonl and known-agents.jsonl.

Port of the config helpers duplicated across scialect's tell-worker.mts,
local-status.mts, local-step.mts and for-all.mts.  As in scialect, the
"control dir" is the current directory by default; it holds workers.jsonl
(one {"id","dir","session","window"} per line), optionally known-agents.jsonl,
and a git-tracked rules/ directory of prompt guides.
"""
import json
import os
from dataclasses import dataclass
from pathlib import Path

# scialect's known-agents.jsonl, used when the control dir has none.
DEFAULT_KNOWN_AGENTS = [
    {"name": "codex", "match": {"args_contains": "codex"}},
    {"name": "claude", "match": {"command": "claude.exe"}},
    {"name": "claude", "match": {"command": "claude"}},
    {"name": "gemini", "match": {"command": "agy"}},
    {"name": "opencode", "match": {"command": "opencode"}},
    {"name": "opencode", "match": {"title_contains": "OC |"}},
]


@dataclass
class WorkerConfig:
    id: str
    dir: str
    session: str = ''
    window: str = ''

    @property
    def path(self):
        return expand_home(self.dir)

    @property
    def target(self):
        """Main pane of the worker's window, e.g. 'jc:3.0'."""
        return f"{self.session}:{self.window}.0"

    @property
    def sci_dir(self):
        return os.path.join(self.path, '.sci')


def expand_home(p):
    if p == '~' or p.startswith('~/'):
        return os.path.expanduser(p)
    return p


def control_dir(path=None):
    return Path(os.path.expanduser(path)) if path else Path.cwd()


def _read_jsonl(path):
    with open(path, 'r', encoding='utf-8') as f:
        return [json.loads(line) for line in (l.strip() for l in f) if line]


def load_workers(cdir=None):
    """Read workers.jsonl from the control dir. Raises FileNotFoundError."""
    rows = _read_jsonl(control_dir(cdir) / 'workers.jsonl')
    known = set(WorkerConfig.__dataclass_fields__)
    return [WorkerConfig(**{k: str(v) for k, v in r.items() if k in known}) for r in rows]


def find_worker(worker_id, cdir=None):
    for w in load_workers(cdir):
        if w.id == worker_id:
            return w
    return None


def load_known_agents(cdir=None):
    """known-agents.jsonl from the control dir, else scialect's defaults."""
    try:
        return _read_jsonl(control_dir(cdir) / 'known-agents.jsonl')
    except (OSError, ValueError):
        return list(DEFAULT_KNOWN_AGENTS)


def read_status_line(worker_or_dir):
    """Full trimmed contents of <dir>/.sci/status-line ('' if missing)."""
    d = worker_or_dir.path if isinstance(worker_or_dir, WorkerConfig) else expand_home(worker_or_dir)
    try:
        with open(os.path.join(d, '.sci', 'status-line'), encoding='utf-8') as f:
            return f.read().strip()
    except OSError:
        return ''


def write_status_line(worker_or_dir, text):
    d = worker_or_dir.path if isinstance(worker_or_dir, WorkerConfig) else expand_home(worker_or_dir)
    with open(os.path.join(d, '.sci', 'status-line'), 'w', encoding='utf-8') as f:
        f.write(text + '\n')
