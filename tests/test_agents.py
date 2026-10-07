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


def test_send_text_and_new_conversation_sequence():
    pane = FakePane(CLAUDE_BLANK)
    sleeps = []
    tui = agents.ClaudeTui('t', capture=pane.capture, send_literal=pane.literal,
                           send_key=pane.key, sleep=sleeps.append)
    tui.new_conversation()
    tui.send_text('hello')
    assert pane.sent == [('lit', '/new'), ('key', 'Enter'), ('lit', 'hello'), ('key', 'Enter')]
    assert sleeps == [0.5, 10.0, 0.5]


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
