"""Run a shell command in every worker directory (port of scialect src/for-all.mts)."""
import subprocess
import sys

from .workers import load_workers


def main(argv, cdir=None):
    if not argv:
        print("Usage: swarm -c for-all '<cmd>'", file=sys.stderr)
        return 1
    cmd = ' '.join(argv)
    try:
        workers = load_workers(cdir)
    except (OSError, ValueError) as e:
        print(f"Failed to read workers.jsonl: {e}", file=sys.stderr)
        return 1
    for w in workers:
        print(f"\n=== Running in {w.id} ({w.path}) ===", flush=True)
        r = subprocess.run(cmd, shell=True, cwd=w.path)
        if r.returncode != 0:
            print(f"Command failed in {w.id} with exit code {r.returncode}", file=sys.stderr)
    print('\n=== Done ===')
    return 0
