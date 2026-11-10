"""A tmux stand-in with one bash pane, for the start_agent shell tests.

It models what the launcher reads: the screen rows (with soft wraps), the cursor, the
history size and the pending input buffer. send-keys -l appends to the buffer (when echo
is on) and Enter submits it. It can be passed as `run` or patched over subprocess.run.
"""
import re
import shlex
import subprocess


class FakeShell:
    def __init__(self, fg='bash', pending='', cwd=None, panes=1, tail='$ ', windows=(), width=80,
                 height=24, echo=True, header='me@host:~/proj (b)', below='', pane='%99'):
        self.calls, self.entered = [], []
        self.fg, self.pending, self.cwd, self.panes, self.tail = fg, pending, cwd, panes, tail
        self.windows, self.width, self.height, self.echo = list(windows), width, height, echo
        self.header, self.below, self.pane = header, below, pane
        self.options = {}

    # -- the screen -------------------------------------------------------------------
    def rows(self):
        """[(text, wrapped)] of every row, history first; the cursor ends the input line."""
        out = [(self.header, False)]
        line = self.tail + self.pending
        chunks = [line[i:i + self.width] for i in range(0, len(line), self.width)] or ['']
        if len(chunks[-1]) == self.width:
            chunks.append('')
        out += [(c, i < len(chunks) - 1) for i, c in enumerate(chunks)]
        self.cursor = (len(out) - 1, len(chunks[-1]))
        out += [(b, False) for b in self.below.split('\n')] if self.below else []
        return out

    def screen(self):
        rows = self.rows()
        hist = max(0, len(rows) - self.height)
        return rows, hist, self.cursor[0] - hist, self.cursor[1]

    def capture(self, args):
        rows, hist, _, _ = self.screen()
        s = int(args[args.index('-S') + 1]) if '-S' in args else 0
        e = int(args[args.index('-E') + 1]) if '-E' in args else self.height - 1
        picked = [rows[i] for i in range(hist + s, hist + e + 1) if 0 <= i < len(rows)]
        if '-J' not in args:
            return ''.join(t.rstrip() + '\n' for t, _ in picked)
        out, cur = [], ''
        for text, wrapped in picked:
            cur += text
            if not wrapped:
                out.append(cur)
                cur = ''
        if cur:
            out.append(cur)
        return ''.join(l + '\n' for l in out)

    def display(self, fmt):
        rows, hist, y, x = self.screen()
        cwd = self.cwd or next((a[a.index('-c') + 1] for a in self.calls if '-c' in a), '/nowhere')
        vals = dict(pane_id=self.pane, pane_current_command=self.fg, window_panes=self.panes,
                    history_size=hist, pane_height=self.height, cursor_y=y, cursor_x=x,
                    pane_current_path=cwd, **self.options)
        return re.sub(r'#\{(@?\w+)\}', lambda m: str(vals.get(m.group(1), '')), fmt) + '\n'

    # -- tmux -------------------------------------------------------------------------
    def __call__(self, argv, **kw):
        self.calls.append(argv)
        sub = argv[1] if len(argv) > 1 else ''
        out = ''
        if sub in ('new-window', 'new-session'):
            out = f'agents\t5\tw\t0\t{self.pane}\t1\t0\tbash\t80\t24\t1\t0\t/x\n'
        elif sub == 'display-message':
            out = self.display(argv[-1])
        elif sub == 'capture-pane':
            out = self.capture(argv)
        elif sub == 'set-option':
            self.options[argv[-2]] = argv[-1]
        elif sub == 'list-panes':
            out = ''.join(f'{w}\t{n}\t{p}\n' for w, n, p in self.windows)
        elif sub == 'send-keys' and '-l' in argv:
            if self.echo:
                self.pending += argv[-1]
        elif sub == 'send-keys' and argv[-1] == 'Enter':
            self.entered.append(self.pending)
            self.pending = ''
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr='')

    def typed(self):
        """The literal texts typed with send-keys -l."""
        return [a[-1] for a in self.calls if a[:2] == ['tmux', 'send-keys'] and '-l' in a]

    def typed_argv(self):
        return [shlex.split(t) for t in FakeShell.typed(self)]

    def new_windows(self):
        return [a for a in self.calls if len(a) > 1 and a[1] in ('new-window', 'new-session')]
