from tangentswarm import agents

BAR = '─' * 60

CLAUDE_BLANK = f"""some output
{BAR}
❯ 
{BAR}
  ? for shortcuts
"""
CLAUDE_TYPED = CLAUDE_BLANK.replace('❯ ', '❯ fix the tests please')
CODEX_BLANK = "header\n› \n  ⏎ send\n"
CODEX_TYPED = "header\n› explain this\n"
GEMINI_BLANK = f"{BAR}\n>   \n{BAR}\n"
GEMINI_TYPED = f"{BAR}\n> old\n> hello\n{BAR}\n"


def test_claude_detector():
    assert agents.claude_prompt_blank(CLAUDE_BLANK)
    assert not agents.claude_prompt_blank(CLAUDE_TYPED)
    assert agents.claude_prompt_blank("no bars\n❯   \n")
    assert not agents.claude_prompt_blank("nothing here")


def test_codex_detector():
    assert agents.codex_prompt_blank(CODEX_BLANK)
    assert not agents.codex_prompt_blank(CODEX_TYPED)


def test_gemini_detector_uses_lowest_prompt():
    assert agents.gemini_prompt_blank(GEMINI_BLANK)
    assert not agents.gemini_prompt_blank(GEMINI_TYPED)


def test_detector_for():
    assert agents.detector_for('codex-cli') is agents.codex_prompt_blank
    assert agents.detector_for('claude') is agents.claude_prompt_blank
    assert agents.detector_for('opencode') is None


class FakePane:
    """Screen that turns blank when a space is typed over placeholder text."""

    def __init__(self, screen, placeholder=False):
        self.screen = screen
        self.placeholder = placeholder
        self.sent = []

    def capture(self, target):
        return self.screen

    def literal(self, target, s):
        self.sent.append(('lit', s))
        if s == ' ' and self.placeholder:
            self.screen = CLAUDE_BLANK

    def key(self, target, k):
        self.sent.append(('key', k))


def make(pane):
    return agents.ClaudeTui('t', capture=pane.capture, send_literal=pane.literal,
                            send_key=pane.key, sleep=lambda s: None)


def test_space_probe_placeholder_counts_as_empty():
    pane = FakePane(CLAUDE_TYPED, placeholder=True)
    assert make(pane).ensure_prompt_is_empty()
    assert pane.sent == [('lit', ' '), ('key', 'BSpace')]


def test_space_probe_real_input_is_not_empty():
    pane = FakePane(CLAUDE_TYPED)
    assert not make(pane).ensure_prompt_is_empty()
    assert pane.sent[-1] == ('key', 'BSpace')


def test_blank_prompt_skips_probe():
    pane = FakePane(CLAUDE_BLANK)
    assert make(pane).ensure_prompt_is_empty()
    assert pane.sent == []


def test_match_agent_rules():
    rules = agents.load_known_agents('/nonexistent')
    assert agents.match_agent(rules, '/usr/bin/claude', '', '') == 'claude'
    assert agents.match_agent(rules, 'node', 'node /x/codex --yolo', '') == 'codex'
    assert agents.match_agent(rules, 'bash', '', 'OC | thing') == 'opencode'
    assert agents.match_agent(rules, 'bash', '-bash', 'title') is None


def test_wait_for_idle_settles_and_times_out():
    t = [0.0]
    screens = iter(['a', 'b'] + [CLAUDE_BLANK] * 100)
    res = agents.wait_for_idle('t', timeout=60, poll=1, settle=3, agent='claude',
                               capture=lambda: next(screens), sleep=lambda s: t.__setitem__(0, t[0] + s),
                               clock=lambda: t[0])
    assert res['idle'] and res['waited_sec'] >= 5
    t[0] = 0.0
    res = agents.wait_for_idle('t', timeout=5, poll=1, settle=3, agent='claude',
                               capture=lambda: CLAUDE_TYPED, sleep=lambda s: t.__setitem__(0, t[0] + s),
                               clock=lambda: t[0])
    assert not res['idle']


# ---------------------------------------------------------------------------
# submitting: send_text checks that the text left the input box

from pathlib import Path   # noqa: E402

import pytest              # noqa: E402

FIX = Path(__file__).parent / 'fixtures'
DIM, RESET = '\x1b[2m', '\x1b[0m'
MSG = 'please run the unit tests and report back'


class BoxPane:
    """An agent's input box. Typed text lands in the box, and Enter submits it unless the
    pane is set to drop that Enter. Captures are escape-coded, as tmux's are, so the
    ghost text is drawn dim. `style` picks the layout: claude (also muse), codex, grok,
    gemini."""

    def __init__(self, style='claude', drop=0, ghost='', dialog_on_enter=False,
                 eat_typing=False, lag=0):
        self.style, self.drop, self.ghost = style, drop, ghost
        self.dialog_on_enter, self.eat_typing, self.lag = dialog_on_enter, eat_typing, lag
        self.buf, self.history, self.dialog = '', [], False
        self.sent, self.submitted, self.dialog_enters = [], [], 0
        self.captures = 0

    def literal(self, target, s):
        self.sent.append(('lit', s))
        if not (self.eat_typing or self.dialog):
            self.buf += s

    def key(self, target, k):
        self.sent.append(('key', k))
        if k != 'Enter':
            return
        if self.dialog:
            self.dialog_enters += 1
        elif self.drop:
            self.drop -= 1
        elif self.dialog_on_enter:
            self.dialog = True
        elif self.buf:
            self.submitted.append(self.buf)
            self.history.append(self.buf)
            self.buf = ''

    def raw(self):
        self.captures += 1
        buf = '' if self.captures <= self.lag else self.buf     # a loaded TUI draws late
        echo = ''.join(f'{self.glyph} {m.splitlines()[0]}\n⏺ ok\n' for m in self.history)
        if self.dialog:
            return echo + f"{'─' * 40}\n Do you want to proceed?\n {self.glyph} 1. Yes\n   2. No\n"
        body = (f'[Pasted text #1 +{buf.count(chr(10))} lines]' if len(buf) > 800
                else buf.replace('\n', '\n  ')) if buf else DIM + self.ghost + RESET
        if self.style == 'codex':
            return f"{echo}\n{self.glyph} {body}\n\n  ⏎ send   ⌃J newline\n"
        if self.style in ('grok', 'gemini'):
            inner = '\n'.join(f'│ {l}' for l in f'{self.glyph} {body}'.split('\n'))
            return f"{echo}╭{'─' * 40}╮\n{inner}\n╰{'─' * 40}╯\n"
        return f"{echo}{'─' * 40}\n{self.glyph} {body}\n{'─' * 40}\n  ? for shortcuts\n"

    @property
    def glyph(self):
        return {'codex': '›', 'gemini': '>'}.get(self.style, '❯')

    def capture(self, target):
        return agents.input_view(self.raw())


def tui(pane, agent='claude', sleeps=None):
    return agents.tui_for(agent, 't', capture=pane.capture, send_literal=pane.literal,
                          send_key=pane.key, sleep=(sleeps.append if sleeps is not None else lambda s: None))


def enters(pane):
    return sum(1 for e in pane.sent if e == ('key', 'Enter'))


def test_send_text_submits_and_verifies():
    pane, sleeps = BoxPane(), []
    out = tui(pane, sleeps=sleeps).send_text(MSG)
    assert out == {'enter_presses': 1, 'submit_verified': True}
    assert pane.submitted == [MSG]
    # 0.5s floor, one poll to see the box settle, then the first check after Enter
    assert sleeps == [agents.ENTER_DELAY, agents.TYPED_POLL, agents.SUBMIT_BACKOFF[0]]


def test_lost_enters_are_pressed_again_with_backoff():
    pane, sleeps = BoxPane(drop=2), []
    out = tui(pane, sleeps=sleeps).send_text(MSG)
    assert out == {'enter_presses': 3, 'submit_verified': True}
    assert pane.submitted == [MSG]
    assert sleeps[2:] == [0.5, 1.0, 2.0]


def test_text_that_never_submits_fails_loudly():
    pane = BoxPane(drop=99)
    with pytest.raises(agents.SubmitError, match='still in claude.s input box after 4 Enter presses') as e:
        tui(pane).send_text(MSG)
    assert e.value.state == 'pending'
    assert any(MSG in l for l in e.value.last_lines)       # the caller sees the stuck text
    assert enters(pane) == 4 and pane.submitted == []
    assert isinstance(e.value, agents.AgentNotReady)       # MCP and the CLI already report these


def test_slow_drawing_box_waits_for_the_text_before_enter():
    pane = BoxPane(lag=6)
    out = tui(pane).send_text(MSG)
    assert out['submit_verified'] and out['enter_presses'] == 1
    assert pane.sent.index(('key', 'Enter')) == 1 and pane.captures >= 8


def test_ghost_text_after_submit_is_not_unsent_input():
    """Claude Code suggests a next prompt in dim grey, and it may read just like ours."""
    pane = BoxPane(ghost=MSG)
    out = tui(pane).send_text(MSG)
    assert out == {'enter_presses': 1, 'submit_verified': True}


def test_ghost_text_is_not_mistaken_for_typed_text():
    """A dialog ate the keystrokes, and the dim ghost shows the same words: no Enter."""
    pane = BoxPane(ghost=MSG, eat_typing=True)
    with pytest.raises(agents.SubmitError, match='never showed up') as e:
        tui(pane).send_text(MSG)
    assert e.value.state == 'not_typed' and enters(pane) == 0


def test_dialog_mid_send_never_gets_an_enter():
    """The first Enter is lost and a permission prompt opens: the box is gone, so stop."""
    pane = BoxPane(drop=1, dialog_on_enter=True)
    with pytest.raises(agents.SubmitError, match='may or may not have been submitted') as e:
        tui(pane).send_text(MSG)
    assert e.value.state == 'unconfirmed'
    assert enters(pane) == 2 and pane.dialog_enters == 0


def test_echo_in_transcript_is_not_the_input_box():
    """After a submit the transcript shows '❯ <message>' above a dialog's '❯ 1. Yes'."""
    pane = BoxPane()
    pane.history, pane.dialog = [MSG], True
    screen = pane.capture('t')
    assert agents.input_box(screen, '❯', framed=True) is None
    assert tui(pane)._submit_state(screen, MSG) == 'unknown'


@pytest.mark.parametrize('agent', ['claude', 'codex'])
@pytest.mark.parametrize('text', ['first line of the message\nsecond line\n\nfourth line',
                                  'hi\n\nthe real request starts on line three'])
def test_multiline_text(agent, text):
    pane = BoxPane(style=agent, drop=1)
    assert tui(pane, agent).send_text(text) == {'enter_presses': 2, 'submit_verified': True}
    assert pane.submitted == [text]


def test_text_with_cr_cannot_submit_part_way():
    pane = BoxPane()
    tui(pane).send_text('one\r\ntwo\rthree')
    assert pane.sent[0] == ('lit', 'one\ntwo\nthree')
    assert pane.submitted == ['one\ntwo\nthree']


def test_long_text_collapsed_to_a_paste_placeholder():
    text = 'x' * 5000 + '\nmore'
    pane = BoxPane()
    out = tui(pane).send_text(text)
    assert out == {'enter_presses': 1, 'submit_verified': True}
    pane = BoxPane(drop=1)
    assert tui(pane).send_text(text)['enter_presses'] == 2


def test_new_conversation_verifies_slash_new():
    pane, sleeps = BoxPane(drop=1), []
    tui(pane, sleeps=sleeps).new_conversation()
    assert pane.submitted == ['/new'] and enters(pane) == 2
    assert sleeps[-1] == agents.NEW_CONVERSATION_WAIT
    pane = BoxPane(drop=99)
    with pytest.raises(agents.SubmitError):
        tui(pane).new_conversation()


@pytest.mark.parametrize('agent', ['claude', 'muse', 'codex', 'grok', 'gemini'])
def test_every_adapter_retries_and_verifies(agent):
    style = agent if agent in ('codex', 'grok', 'gemini') else 'claude'
    pane = BoxPane(style=style, drop=1)
    t = tui(pane, agent)
    assert t.detect(pane.capture('t'))                    # blank box reads as blank
    assert t.send_text(MSG) == {'enter_presses': 2, 'submit_verified': True}
    assert pane.submitted == [MSG]
    with pytest.raises(agents.SubmitError):
        tui(BoxPane(style=style, drop=99), agent).send_text(MSG)


def test_codex_dialog_menu_never_gets_an_enter():
    pane = BoxPane(style='codex', drop=1, dialog_on_enter=True)
    with pytest.raises(agents.SubmitError) as e:
        tui(pane, 'codex').send_text(MSG)
    assert e.value.state == 'unconfirmed' and pane.dialog_enters == 0


def test_unknown_layout_presses_enter_once_unverified():
    pane = BoxPane()
    t = agents.TuiAgent('t', capture=pane.capture, send_literal=pane.literal, send_key=pane.key,
                        sleep=lambda s: None)
    assert t.send_text(MSG) == {'enter_presses': 1, 'submit_verified': False}


# ---------------------------------------------------------------------------
# the box and ghost text on captures from live agents

def view(name):
    return agents.input_view((FIX / name).read_text())


def test_claude_idle_placeholder_is_blank():
    """'Try "how does <filepath> work?"' is dim: its '>' must not survive as a glyph."""
    screen = view('claude_idle_placeholder.ansi')
    assert agents.claude_prompt_blank(screen)
    assert agents.input_box(screen, '❯', framed=True).strip() == ''
    assert agents.tui_for('claude', 't')._submit_state(screen, MSG) == 'sent'


def test_live_captures_input_box():
    claude = agents.tui_for('claude', 't')
    assert claude._submit_state(view('claude_multiline.ansi'),
                                'line one of the message\nline two\nline three') == 'pending'
    assert claude._submit_state(view('claude_pasted.ansi'), 'y' * 9000) == 'pending'
    assert agents.input_box(view('claude_trust_dialog.ansi'), '❯', framed=True) is None
    assert claude._submit_state(view('claude_trust_dialog.ansi'), MSG) == 'unknown'
    codex = agents.tui_for('codex', 't')
    assert codex._submit_state(view('codex_update_dialog.ansi'), MSG) == 'unknown'
    assert agents.tui_for('muse', 't')._submit_state(view('muse_typed.ansi'), MSG) == 'pending'
    grok = agents.tui_for('grok', 't')
    assert grok._submit_state(view('grok_typed.ansi'), MSG) == 'pending'
    assert grok._submit_state(view('grok_idle.ansi'), MSG) == 'sent'
    assert agents.grok_prompt_blank(view('grok_idle.ansi'))
    assert not agents.grok_prompt_blank(view('grok_typed.ansi'))
