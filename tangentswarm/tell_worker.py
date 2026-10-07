"""State-machine handoffs to worker / manager agents (port of scialect src/tell-worker.mts).

    swarm -c tell-worker <worker> assigned|accept|plan-approved|adjust|unblocked|reject
    swarm -c tell-worker <worker> rebase [branch]
    swarm -c tell-worker <manager> review|approve-task|unblock <worker>

Every handoff is atomic, as in scialect: reach an empty prompt and deliver
the message FIRST, and only then write the new .sci/status-line and
propagate the committed prompt guides (rules/<guide> plus its `uses:`
closure, read with `git show HEAD:rules/...` in the control dir).  If the
prompt never empties, nothing is changed and the command is safe to retry.
"""
import os
import subprocess
import sys

from . import agents, tmux
from .rule_deps import resolve_dependencies
from .workers import control_dir, load_workers


class TellWorkerError(RuntimeError):
    pass


USAGE = """Usage:
  swarm -c tell-worker <worker> assigned
  swarm -c tell-worker <worker> accept
  swarm -c tell-worker <worker> plan-approved
  swarm -c tell-worker <worker> adjust
  swarm -c tell-worker <worker> unblocked
  swarm -c tell-worker <worker> reject
  swarm -c tell-worker <manager> review <worker>
  swarm -c tell-worker <manager> approve-task <worker>
  swarm -c tell-worker <manager> unblock <worker>
  swarm -c tell-worker <worker> rebase [branch]"""

WORKER_VERBS = ['assigned', 'accept', 'plan-approved', 'adjust', 'unblocked', 'reject', 'rebase']
MANAGER_VERBS = ['review', 'approve-task', 'unblock']
VERBS = WORKER_VERBS + MANAGER_VERBS


class TellWorker:
    def __init__(self, cdir=None, out=None, tui_factory=None, agent_detector=None, sleep=None):
        self.cdir = control_dir(cdir)
        self.out = out or sys.stdout
        self.tui_factory = tui_factory or agents.tui_for
        self.detect = agent_detector or (lambda w: agents.detect_agent(f"{w.session}:{w.window}", cdir=self.cdir))
        self.log_lines = []

    def log(self, msg):
        self.log_lines.append(msg)
        print(msg, file=self.out, flush=True)

    def fail(self, msg):
        raise TellWorkerError(msg)

    # -- guides --------------------------------------------------------------
    def committed_guide(self, name):
        r = subprocess.run(['git', 'show', f'HEAD:rules/{name}'], cwd=self.cdir,
                           stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False)
        return r.stdout if r.returncode == 0 else None

    def copy_guide(self, target_dir, name):
        content = self.committed_guide(name)
        if not content:
            self.fail(f"Could not retrieve committed content for rules/{name}")
        path = os.path.join(target_dir, '.sci', name)
        existing = open(path, encoding='utf-8').read() if os.path.exists(path) else ''
        if existing.strip() != content.strip():
            with open(path, 'w', encoding='utf-8') as f:
                f.write(content)
            return True
        return False

    def propagate_guide(self, target_dir, name):
        changed = self.copy_guide(target_dir, name)
        for dep in resolve_dependencies(name, self.committed_guide):
            if self.copy_guide(target_dir, dep):
                self.log(f"  {name} → propagated dependency {dep}")
                changed = True
        return changed

    def guide_would_change(self, target_dir, name):
        for n in [name] + resolve_dependencies(name, self.committed_guide):
            committed = self.committed_guide(n)
            if not committed:
                continue
            path = os.path.join(target_dir, '.sci', n)
            existing = open(path, encoding='utf-8').read() if os.path.exists(path) else ''
            if existing.strip() != committed.strip():
                return True
        return False

    # -- checks --------------------------------------------------------------
    @staticmethod
    def _status(w):
        p = os.path.join(w.sci_dir, 'status-line')
        return open(p, encoding='utf-8').read().strip() if os.path.exists(p) else ''

    def check_idle(self, w):
        s = self._status(w)
        if s and not s.startswith('IDLE') and not s.startswith('HELD'):
            self.fail(f"{w.id}: Status is not IDLE or HELD (currently: '{s}'). Cannot hand off new task.")

    def check_ready_for_integration(self, w):
        s = self._status(w)
        u = s.upper()
        if s and not any(u.startswith(p) for p in ('IDLE', 'HELD', 'AWAITING: CODE REVIEW', 'READY')):
            self.fail(f"{w.id}: Status is not ready for integration (currently: '{s}'). Cannot start rebase.")

    def assert_window(self, w):
        try:
            panes = tmux.list_panes(f"{w.session}:{w.window}")
        except tmux.TmuxError:
            panes = []
        if not panes:
            self.fail(f"{w.id}: tmux window '{w.session}:{w.window}' does not exist")

    @staticmethod
    def git_clean(d):
        r = subprocess.run(['git', 'status', '--porcelain'], cwd=d, stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, check=False)
        return r.returncode == 0 and not r.stdout.strip()

    @staticmethod
    def write_status(w, text):
        with open(os.path.join(w.sci_dir, 'status-line'), 'w', encoding='utf-8') as f:
            f.write(text + '\n')

    def _tui(self, w, target, default):
        agent = (self.detect(w) or default).lower()
        return agent, self.tui_factory(agent, target)

    def _reach_empty_prompt(self, label, tui):
        self.log(f"{label}: waiting for empty prompt (up to 5s)...")
        if not tui.ensure_prompt_is_empty():
            self.fail(f"{label}: never reached empty prompt — no changes made; worker left as-is, safe to retry.")

    # -- handoffs ------------------------------------------------------------
    def send_then_commit(self, w, target, message, commit, guide=None):
        """sendHandoffThenCommit."""
        agent, tui = self._tui(w, target, 'claude')
        if tui is None:
            self.log(f"{w.id}: no special TUI handling for agent '{agent}' yet.")
            commit()
            return
        self._reach_empty_prompt(w.id, tui)
        notice = (f" IMPORTANT: .sci/{guide} has just been updated with new instructions; please read it carefully."
                  if guide and self.guide_would_change(w.path, guide) else '')
        tui.send_text(message(notice))
        commit()
        self.log(f"{w.id}: handoff sent to {agent}.")

    def finalize_assigned(self, w):
        for name in ('result.md', 'review.md'):
            p = os.path.join(w.sci_dir, name)
            if os.path.exists(p):
                os.unlink(p)
        self.propagate_guide(w.path, 'proving-guide.md')
        self.write_status(w, 'ASSIGNED')
        self.log(f"{w.id}: files prepared (ASSIGNED, .sci/goal.md + plan.md + task.md, result cleared, guide propagated)")

    def do_assigned(self, w):
        self.check_idle(w)
        self.assert_window(w)
        if not self.git_clean(w.path):
            self.fail(f"{w.id}: git tree is not clean")
        for name in ('goal.md', 'task.md'):
            if not os.path.exists(os.path.join(w.sci_dir, name)):
                self.fail(f"{w.id}: .sci/{name} does not exist")
        agent, tui = self._tui(w, w.target, 'claude')
        if tui is None:
            self.log(f"{w.id}: no special TUI handling for agent '{agent}' yet.")
            self.finalize_assigned(w)
            return
        self._reach_empty_prompt(w.id, tui)
        notice = (' IMPORTANT: .sci/proving-guide.md has just been updated with new instructions; please read it carefully.'
                  if self.guide_would_change(w.path, 'proving-guide.md') else '')
        self.log(f"{w.id}: clean prompt detected. Sending handoff...")
        msg = ('/goal You have been assigned a new task. Please review .sci/goal.md, .sci/plan.md, and .sci/task.md. '
               'Then, follow the instructions in .sci/proving-guide.md to acknowledge the assignment and begin work.'
               + notice)
        if agent == 'claude' or 'codex' in agent:
            tui.new_conversation()      # /new, 500ms, Enter, 10s
        tui.send_text(msg)              # text, 500ms, Enter
        self.finalize_assigned(w)
        self.log(f"{w.id}: handoff sent to {agent}.")

    def do_accept(self, w):
        self.assert_window(w)

        def commit():
            self.propagate_guide(w.path, 'proving-guide.md')
            self.write_status(w, 'WORKING: plan next step')
            self.log(f"{w.id}: files prepared (WORKING: plan next step, guide propagated)")
        self.send_then_commit(w, w.target, lambda n: (
            'Your recent work has been accepted! Please read your .sci/plan.md, formulate your next commit-sized '
            'step in .sci/task.md, and set your status to SUGGEST when your task plan is ready for manager approval. '
            f'Refer to .sci/proving-guide.md for detailed instructions.{n}'), commit, 'proving-guide.md')

    def do_plan_approved(self, w):
        self.assert_window(w)

        def commit():
            self.write_status(w, 'WORKING: starting task')
            self.log(f"{w.id}: files prepared (WORKING: starting task)")
        self.send_then_commit(w, w.target, lambda n: (
            'Your proposed task plan has been approved by the manager! Please begin executing your plan. You may use '
            'tools to write code, test it, and commit it. Remember to set your status to READY when finished.'), commit)

    def do_adjust(self, w):
        self.assert_window(w)
        if not os.path.exists(os.path.join(w.sci_dir, 'review.md')):
            self.fail(f"{w.id}: no .sci/review.md — the manager must write adjustment feedback there before 'adjust'. No changes made.")

        def commit():
            self.propagate_guide(w.path, 'adjust-guide.md')
            self.write_status(w, 'WORKING: adjust task plan')
            self.log(f"{w.id}: files prepared (WORKING: adjust task plan, guide propagated)")
        self.send_then_commit(w, w.target, lambda n: (
            "Your proposed task plan was not approved by the manager. Please adjust the task in .sci/task.md based on "
            f"the manager's feedback. Refer to .sci/adjust-guide.md for detailed instructions.{n}"), commit, 'adjust-guide.md')

    def do_unblocked(self, w):
        self.assert_window(w)

        def commit():
            self.write_status(w, 'WORKING: resume task')
            self.log(f"{w.id}: files prepared (WORKING: resume task)")
        self.send_then_commit(w, w.target, lambda n: (
            'The manager has triaged your blocker. Please read .sci/task.md to see their resolution or instructions, '
            'adjust your approach as directed, and resume working on your task. Remember to set your status back to '
            'READY when finished.'), commit)

    def do_reject(self, w):
        self.assert_window(w)

        def commit():
            self.propagate_guide(w.path, 'adjust-guide.md')
            self.write_status(w, 'WORKING: fix rejected code')
            self.log(f"{w.id}: files prepared (WORKING: fix rejected code, guide propagated)")
        self.send_then_commit(w, w.target, lambda n: (
            "The manager has REJECTED your code! Please read the manager's review in .sci/review.md, revert any bad "
            "commits if necessary, fix your code, and submit it again. Remember to set your status back to READY when "
            f"finished.{n}"), commit, 'adjust-guide.md')

    def do_rebase(self, w, branch='origin/main'):
        self.check_ready_for_integration(w)
        self.assert_window(w)

        def commit():
            self.propagate_guide(w.path, 'rebase-guide.md')
            self.write_status(w, f'WORKING: rebase onto {branch}')
            self.log(f"{w.id}: files prepared (WORKING: rebase onto {branch}, guide propagated)")
        self.send_then_commit(w, w.target, lambda n: (
            f'It is time to integrate your work! Please fetch and rebase your branch onto {branch}. Resolve any '
            'conflicts if they occur. Then run local checks (lake build Jacobian.Solution, python3 '
            'scripts/blueprint_audit.py, python3 scripts/blueprint_graph_audit.py). If everything passes, force push '
            'your branch to GitHub and create a pull request using the gh CLI. Refer to .sci/rebase-guide.md for '
            f'detailed instructions.{n}'), commit, 'rebase-guide.md')

    def _manager_request(self, mgr, target_worker_id, guide, what, build_message):
        self.check_idle(mgr)
        self.assert_window(mgr)
        target = mgr.target
        agent = (self.detect(mgr) or 'unknown').lower()
        self.log(f"{mgr.id}: detected agent = {agent}")
        tw = next((x for x in load_workers(self.cdir) if x.id == target_worker_id), None)
        if tw is None:
            self.fail(f"{mgr.id}: Unknown target worker: {target_worker_id}")
        tui = self.tui_factory(agent, target)
        if tui is None:
            self.fail(f"{mgr.id}: no TUI handler implemented for agent '{agent}'")
        notice = (f" IMPORTANT: your own {guide[:-3]} at {mgr.path}/.sci/{guide} has just been updated with new "
                  "instructions; please read it carefully." if self.guide_would_change(mgr.path, guide) else '')
        msg = build_message(tw.path, notice)
        self.log(f"{mgr.id}: waiting for empty prompt (up to 5s)...")
        if not tui.ensure_prompt_is_empty():
            self.fail(f"{mgr.id}: never reached empty prompt — no changes made; safe to retry.")
        self.log(f"{mgr.id}: sending {what} for {target_worker_id}...")
        tui.send_text(msg)
        self.propagate_guide(mgr.path, guide)
        self.write_status(mgr, f'REVIEWING: {target_worker_id}')
        self.log(f"{mgr.id}: {what} sent for {target_worker_id}.")

    def do_review(self, mgr, wid):
        m = mgr.path
        self._manager_request(mgr, wid, 'review-guide.md', 'review request', lambda td, n: (
            f"Please review the completed code task for {wid}. Change your directory to the worker's project directory "
            f"{td} and inspect the files there: read that worker's {td}/.sci/goal.md, {td}/.sci/plan.md, and "
            f"{td}/.sci/task.md, and write your review to that worker's {td}/.sci/review.md. Do not overwrite that "
            f"worker's {td}/.sci/result.md; it is reserved for worker task output. Follow the instructions in YOUR OWN "
            f"review-guide at {m}/.sci/review-guide.md, and set YOUR OWN status-line at {m}/.sci/status-line (NOT the "
            f"worker's).{n}"))

    def do_approve_task(self, mgr, wid):
        m = mgr.path
        self._manager_request(mgr, wid, 'approve-task-guide.md', 'task approval request', lambda td, n: (
            f"Please review and approve the proposed next task plan for {wid}. Change your directory to the worker's "
            f"project directory {td} and inspect the files there: read that worker's {td}/.sci/task.md and "
            f"{td}/.sci/plan.md. Follow the instructions in YOUR OWN approve-task-guide at "
            f"{m}/.sci/approve-task-guide.md, and set YOUR OWN status-line at {m}/.sci/status-line (NOT the worker's).{n}"))

    def do_unblock(self, mgr, wid):
        m = mgr.path
        self._manager_request(mgr, wid, 'unblock-guide.md', 'unblock request', lambda td, n: (
            f"Please triage the blocker reported by {wid}. Change your directory to the worker's project directory "
            f"{td} and inspect that worker's {td}/.sci/task.md there, where you should also write your triage "
            f"feedback. Follow the instructions in YOUR OWN unblock-guide at {m}/.sci/unblock-guide.md, and set YOUR "
            f"OWN status-line at {m}/.sci/status-line (NOT the worker's).{n}"))

    # -- dispatch ------------------------------------------------------------
    def run(self, worker_id, action, *args):
        workers = load_workers(self.cdir)
        w = next((x for x in workers if x.id == worker_id), None)
        if w is None:
            self.fail(f"Unknown worker: {worker_id}")
        if action in MANAGER_VERBS:
            if not args or not args[0]:
                self.fail(f"Usage: swarm -c tell-worker <manager> {action} <worker>")
            {'review': self.do_review, 'approve-task': self.do_approve_task,
             'unblock': self.do_unblock}[action](w, args[0])
        elif action == 'rebase':
            self.do_rebase(w, args[0] if args and args[0] else 'origin/main')
        elif action in WORKER_VERBS:
            {'assigned': self.do_assigned, 'accept': self.do_accept, 'plan-approved': self.do_plan_approved,
             'adjust': self.do_adjust, 'unblocked': self.do_unblocked, 'reject': self.do_reject}[action](w)
        else:
            self.fail(f"Unknown action: {action}")
        return self.log_lines


def main(argv, cdir=None):
    if len(argv) < 2:
        print(USAGE, file=sys.stderr)
        return 1
    try:
        TellWorker(cdir).run(*argv)
    except TellWorkerError as e:
        print(str(e), file=sys.stderr)
        return 1
    return 0
