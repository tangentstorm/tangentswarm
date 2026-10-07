"""Advance the swarm one handoff (port of scialect src/local-step.mts).

Reads every worker's .sci/status-line, proposes the next legal transition(s)
and runs the chosen one after an interactive y/N (or number) confirmation.
Interactive CLI only -- deliberately not exposed as an MCP tool, because the
PR-AWAIT step merges a pull request with `gh pr merge` once a human confirms.
"""
import random
import re
import subprocess
import sys

from . import local_status
from .tell_worker import TellWorker, TellWorkerError
from .workers import load_workers, read_status_line, write_status_line


def _tell(cdir, *args):
    try:
        TellWorker(cdir).run(*args)
    except TellWorkerError as e:
        print(f"Command failed: {e}", file=sys.stderr)


def propose(cdir=None):
    """Return a list of (description, callable) proposals and the pending list."""
    workers = load_workers(cdir)
    mgr = next((w for w in workers if w.id == 'mgr'), None)
    if mgr is None:
        raise RuntimeError("Could not find 'mgr' in workers.jsonl")
    ordinary = [w for w in workers if w.id != 'mgr']
    mgr_status = read_status_line(mgr)
    mgr_idle = mgr_status.upper().startswith('IDLE')
    proposals = []

    def reset_mgr_then(*args):
        def run():
            print('Resetting manager status to IDLE...')
            write_status_line(mgr, 'IDLE: ...')
            print(f"Running tell-worker {' '.join(args[1:])} for {args[0]}...")
            _tell(cdir, *args)
        return run

    m = re.match(r'^REVIEWED:\s*(ACCEPT|PREPARE|ADJUST|REJECT|UNBLOCKED)\s+([A-Za-z0-9_-]+)', mgr_status, re.I)
    if m:
        decision, tid = m.group(1).upper(), m.group(2)
        tw = next((w for w in ordinary if w.id == tid), None)
        if tw:
            if decision == 'ACCEPT':
                if 'PLAN APPROVAL' in read_status_line(tw).upper():
                    proposals.append((f"[Decision] Manager approved {tid}'s task plan. Transition {tid} to execution and reset manager to IDLE.",
                                      reset_mgr_then(tid, 'plan-approved')))
                else:
                    proposals.append((f"[Decision] Manager accepted {tid}'s code work. Transition {tid} to planning and reset manager to IDLE.",
                                      reset_mgr_then(tid, 'accept')))
            elif decision == 'PREPARE':
                proposals.append((f"[Decision] Manager accepted {tid}'s code work for integration. Start merge preparation and reset manager to IDLE.",
                                  reset_mgr_then(tid, 'rebase', 'origin/main')))
            elif decision == 'ADJUST':
                proposals.append((f"[Decision] Manager requested adjustments for {tid}'s plan. Transition {tid} to adjusting and reset manager to IDLE.",
                                  reset_mgr_then(tid, 'adjust')))
            elif decision == 'REJECT':
                proposals.append((f"[Decision] Manager REJECTED {tid}'s work. Transition {tid} back to WORKING and reset manager to IDLE.",
                                  reset_mgr_then(tid, 'reject')))
            elif decision == 'UNBLOCKED':
                proposals.append((f"[Decision] Manager resolved blocker for {tid}. Transition {tid} back to WORKING and reset manager to IDLE.",
                                  reset_mgr_then(tid, 'unblocked')))

    def handoff(w, awaiting, verb):
        def run():
            print(f"Setting {w.id} status to {awaiting}...")
            write_status_line(w, awaiting)
            print(f"Running tell-worker {verb} for {w.id}...")
            _tell(cdir, 'mgr', verb, w.id)
        return run

    def pr_merge(w):
        def run():
            print(f"Checking PR checks for {w.id}...")
            chk = subprocess.run(['gh', 'pr', 'checks'], cwd=w.path, capture_output=True, text=True)
            if chk.returncode == 0:
                print(chk.stdout)
                print('\n✅ CI passed! Merging PR...')
                mr = subprocess.run(['gh', 'pr', 'merge', '--merge', '--delete-branch'], cwd=w.path)
                if mr.returncode == 0:
                    print('Setting status to MERGED.')
                    write_status_line(w, 'MERGED: integrated successfully')
                else:
                    print('Failed to merge PR.', file=sys.stderr)
            else:
                print(chk.stdout or chk.stderr)
                print(f"\n⏳ CI is pending or failed (exit code {chk.returncode}). Will check again later.")
        return run

    if mgr_idle:
        for w in ordinary:
            s = read_status_line(w).upper()
            if s.startswith('READY') or s.startswith('STEP-DONE'):
                proposals.append((f"[Handoff] Worker {w.id} is READY. Transition {w.id} to AWAITING and hand off code review to manager.",
                                  handoff(w, 'AWAITING: code review', 'review')))
            elif s.startswith('SUGGEST'):
                proposals.append((f"[Handoff] Worker {w.id} has suggested a new plan (SUGGEST). Transition {w.id} to AWAITING and hand off task plan approval to manager.",
                                  handoff(w, 'AWAITING: task plan approval', 'approve-task')))
            elif s.startswith('BLOCKED'):
                proposals.append((f"[Handoff] Worker {w.id} is BLOCKED. Transition {w.id} to AWAITING and hand off blocker triage to manager.",
                                  handoff(w, 'AWAITING: blocker triage', 'unblock')))
            elif s.startswith('PR-AWAIT'):
                proposals.append((f"[CI] Worker {w.id} is PR-AWAIT. Check if CI is green and merge PR.", pr_merge(w)))

    active = any(read_status_line(w).upper().startswith(('WORKING: REBASE', 'PR-AWAIT')) for w in ordinary)
    if not active:
        held = sorted((w for w in ordinary if read_status_line(w).upper().startswith('HELD')), key=lambda w: w.id)
        if held:
            nw = held[0]

            def start(nw=nw):
                print(f"Starting rebase sequence for {nw.id}...")
                _tell(cdir, nw.id, 'rebase', 'origin/main')
            proposals.append((f"[Integrate] Integration pipeline is empty. Start rebasing {nw.id} onto origin/main.", start))

    pending = []
    if not mgr_idle:
        for w in ordinary:
            s = read_status_line(w)
            if s.upper().startswith(('READY', 'STEP-DONE', 'SUGGEST', 'BLOCKED')):
                pending.append((w.id, s))
    return proposals, pending


def main(argv=None, cdir=None, input_fn=input):
    print('\n=== CURRENT SWARM STATUS ===')
    local_status.print_swarm_table(local_status.collect_swarm_rows(cdir))
    print('============================\n')
    try:
        proposals, pending = propose(cdir)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    if pending:
        print('⚠️  PENDING WORKERS (AWAITING MANAGER TO BE IDLE):')
        for wid, s in pending:
            print(f"- {wid} is currently {s}")
        print('')
    if not proposals:
        print('No actionable transitions detected.')
        return 0
    if len(proposals) == 1:
        desc, run = proposals[0]
        print('PROPOSED ACTION:')
        print(f"  {desc}\n")
        ans = input_fn('Proceed with this action? (y/N): ').strip()
        if ans.lower() == 'y' or ans == '1':
            print('\nExecuting action...')
            run()
            print('Done!')
        else:
            print('\nAction cancelled.')
        return 0
    print('MULTIPLE PROPOSED ACTIONS DETECTED:')
    for i, (desc, _) in enumerate(proposals, 1):
        print(f"  {i}) {desc}")
    print('')
    choice = input_fn(f"Select an action to execute (1-{len(proposals)}), 'y' for a random pick, or 'q' to quit: ").strip().lower()
    if choice == 'q':
        print('\nExiting.')
    elif choice == 'y':
        num = random.randint(1, len(proposals))
        print(f"\nRandomly selected action {num}. Executing...")
        proposals[num - 1][1]()
        print('Done!')
    else:
        try:
            num = int(choice)
        except ValueError:
            num = 0
        if 1 <= num <= len(proposals):
            print(f"\nExecuting action {num}...")
            proposals[num - 1][1]()
            print('Done!')
        else:
            print('\nInvalid choice.')
    return 0
