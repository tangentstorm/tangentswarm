"""A stand-in for Claude Code's input box, for tests against a real tmux pane.

    fake_tui.py LOG [--drop N] [--ghost TEXT] [--dialog-on-enter]

It draws a ❯ input box between two ─ bars, with TEXT as dim ghost text when the box is
empty, and echoes each submitted message above the box as '❯ message'. It reads raw
keys: CR submits, LF is a newline, DEL is a backspace, C-u clears. --drop N ignores the
first N Enters (what an overloaded TUI does). --dialog-on-enter opens a menu on the
first Enter instead of submitting, as a permission prompt would. Every event goes to
LOG, one per line: 'submit <repr>', 'drop', 'dialog', 'dialog-enter'.
"""
import argparse
import os
import sys
import termios
import tty

BAR = '\x1b[38;5;244m' + '─' * 70 + '\x1b[39m'
DIM = '\x1b[2m'
RESET = '\x1b[0m'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('log')
    ap.add_argument('--drop', type=int, default=0)
    ap.add_argument('--ghost', default='')
    ap.add_argument('--dialog-on-enter', action='store_true')
    opt = ap.parse_args()

    log = open(opt.log, 'a', buffering=1, encoding='utf-8')
    buf, history, dialog = '', [], False
    drops = opt.drop

    def draw():
        out = ['\x1b[H\x1b[2J']
        for msg in history[-5:]:
            out.append('❯ ' + msg.split('\n')[0] + '\r\n')
            out.append('⏺ ok\r\n')
        if dialog:
            out += [BAR, '\r\n', ' Do you want to proceed?\r\n', ' ❯ 1. Yes\r\n', '   2. No\r\n',
                    ' Enter to confirm · Esc to cancel\r\n']
        else:
            out += [BAR, '\r\n']
            if not buf:
                out.append('❯ ' + (DIM + opt.ghost + RESET if opt.ghost else '') + '\r\n')
            elif len(buf) > 800:
                out.append('❯ [Pasted text #1 +%d lines]\r\n' % buf.count('\n'))
            else:
                lines = buf.split('\n')
                out.append('❯ ' + lines[0] + '\r\n')
                out += ['  ' + l + '\r\n' for l in lines[1:]]
            out += [BAR, '\r\n', '  ? for shortcuts']
        os.write(1, ''.join(out).encode())

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    tty.setraw(fd)
    try:
        draw()
        pending = b''
        while True:
            data = os.read(fd, 65536)
            if not data:
                break
            pending += data
            try:
                text = pending.decode('utf-8')
                pending = b''
            except UnicodeDecodeError:
                continue
            for ch in text:
                if dialog:
                    if ch == '\r':
                        log.write('dialog-enter\n')
                    continue
                if ch == '\r':
                    if drops > 0:
                        drops -= 1
                        log.write('drop\n')
                    elif opt.dialog_on_enter:
                        dialog = True
                        log.write('dialog\n')
                    elif buf:
                        log.write('submit %r\n' % buf)
                        history.append(buf)
                        buf = ''
                elif ch == '\x7f':
                    buf = buf[:-1]
                elif ch == '\x15':
                    buf = ''
                elif ch == '\x03':
                    return
                else:
                    buf += ch
            draw()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


if __name__ == '__main__':
    main()
