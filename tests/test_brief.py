"""swarm-brief: the brief section in a PR body, the brief files, and the swarm-tick step."""
import json

import pytest

from tangentswarm import brief, tick

BRIEF = "Task #440: disable Record on the title.\nAdd a where-to-see line and a screenshot.png later.\n"


@pytest.fixture
def briefs(tmp_path, monkeypatch):
    monkeypatch.setattr(brief, 'BRIEF_DIR', tmp_path)
    return tmp_path


def test_with_brief_appends_section_after_body():
    body = brief.with_brief('Fixes #440', BRIEF)
    assert body.startswith('Fixes #440\n\n' + brief.START)
    assert brief.has_brief(body) and 'Task #440' in body
    assert body.rstrip().endswith(brief.END)


def test_with_brief_on_empty_body():
    body = brief.with_brief('', BRIEF)
    assert body.startswith(brief.START) and brief.has_brief(body)


def test_with_brief_replaces_not_duplicates():
    once = brief.with_brief('Fixes #440\n\nAfter: more text', 'old brief')
    twice = brief.with_brief(once, 'new brief')
    assert twice.count(brief.START) == 1 and 'new brief' in twice and 'old brief' not in twice
    assert 'After: more text' in twice


def test_stray_markers_in_the_brief_do_not_break_the_section():
    body = brief.with_brief('Fixes #1', f"a {brief.END} b {brief.START} c")
    assert body.count(brief.START) == 1 and body.count(brief.END) == 1
    assert brief.strip_brief(body).strip() == 'Fixes #1'


def test_strip_brief_hides_gate_words_from_body_checks():
    # The brief names a screenshot and a where-to-see line before they exist; a gate
    # check reading the body must not count them.
    body = brief.with_brief('Fixes #440', BRIEF) + '\nreal tail'
    stripped = brief.strip_brief(body)
    assert 'where-to-see' not in stripped and '.png' not in stripped
    assert stripped.startswith('Fixes #440') and 'real tail' in stripped


def test_read_brief_needs_a_non_empty_file(briefs):
    assert brief.read_brief(7) is None
    (briefs / '7.txt').write_text('  \n')
    assert brief.read_brief(7) is None
    (briefs / '7.txt').write_text(BRIEF)
    assert brief.read_brief(7) == BRIEF


def test_missing_briefs_reports_file_presence(briefs):
    (briefs / '2.txt').write_text(BRIEF)
    prs = {1: {'body': 'Fixes #9'}, 2: {'body': 'Fixes #8'}, 3: {'body': brief.with_brief('x', BRIEF)}}
    assert brief.missing_briefs(prs) == [(1, False), (2, True)]


class FakeGh:
    """gh pr view / gh api PATCH against an in-memory body."""

    def __init__(self, body):
        self.body, self.patches = body, 0

    def __call__(self, *args, input=None):
        if args[:2] == ('pr', 'view'):
            return json.dumps({'body': self.body})
        assert args[:3] == ('api', '-X', 'PATCH')
        self.patches += 1
        self.body = json.loads(input)['body']
        return '{}'


def test_write_brief_patches_once(briefs, monkeypatch):
    (briefs / '7.txt').write_text(BRIEF)
    gh = FakeGh('Fixes #440')
    monkeypatch.setattr(brief, '_gh', gh)
    assert brief.write_brief(7) == 'wrote'
    assert brief.has_brief(gh.body) and gh.body.startswith('Fixes #440')
    assert brief.write_brief(7) == 'present' and gh.patches == 1


def test_write_brief_without_file_changes_nothing(briefs, monkeypatch):
    gh = FakeGh('Fixes #440')
    monkeypatch.setattr(brief, '_gh', gh)
    assert brief.write_brief(7) == 'no-file' and gh.patches == 0


def test_write_brief_skips_when_body_gained_a_brief_meanwhile(briefs, monkeypatch):
    # The board read is stale; the live body already has the section.
    (briefs / '7.txt').write_text(BRIEF)
    gh = FakeGh(brief.with_brief('Fixes #440', 'written by hand'))
    monkeypatch.setattr(brief, '_gh', gh)
    assert brief.write_brief(7) == 'present' and gh.patches == 0


def test_cli_file_saves_then_writes(briefs, monkeypatch, tmp_path, capsys):
    src = tmp_path / 'src.md'
    src.write_text(BRIEF)
    gh = FakeGh('Fixes #440')
    monkeypatch.setattr(brief, '_gh', gh)
    assert brief.main(['7', '--file', str(src)]) == 0
    assert (briefs / '7.txt').read_text() == BRIEF and brief.has_brief(gh.body)
    assert 'wrote the brief' in capsys.readouterr().out


def test_cli_refuses_empty_file(briefs, tmp_path):
    src = tmp_path / 'empty.md'
    src.write_text('\n')
    with pytest.raises(SystemExit):
        brief.main(['7', '--file', str(src)])
    assert not (briefs / '7.txt').exists()


# --- the swarm-tick step ------------------------------------------------------

def make_tick(prs, dry=True):
    t = tick.Tick.__new__(tick.Tick)
    t.dry, t.verbose, t.out, t.did, t.notes = dry, False, print, [], []
    t.prs, t.panes = prs, []
    return t


def pr(n, col='In Progress', body='Fixes #1', **kw):
    return dict(number=n, state='OPEN', draft=True, col=col, body=body, parked=False, rank=1, **kw)


def test_tick_brief_pass_writes_missing_and_notes_no_file(briefs, monkeypatch):
    for n in (1, 2, 5, 6):
        (briefs / f'{n}.txt').write_text(BRIEF)
    prs = {1: pr(1), 2: pr(2, col='Auto Review'), 3: pr(3), 4: pr(4, body=brief.with_brief('x', BRIEF)),
           5: pr(5, col='Pending'), 6: dict(pr(6), parked=True)}
    t = make_tick(prs)
    t.brief_pass()
    assert t.did == ['would: #1 write the brief into the PR body', 'would: #2 write the brief into the PR body']
    assert [n for n, _ in t.notes] == [3] and 'no brief file' in t.notes[0][1]


def test_tick_brief_pass_live_calls_write_brief(briefs, monkeypatch):
    (briefs / '1.txt').write_text(BRIEF)
    gh = FakeGh('Fixes #440')
    monkeypatch.setattr(brief, '_gh', gh)
    monkeypatch.setattr(tick, 'log', lambda line: None)
    t = make_tick({1: pr(1)}, dry=False)
    t.brief_pass()
    assert t.did == ['did: #1 wrote the brief into the PR body'] and brief.has_brief(gh.body)


def test_tick_brief_pass_reports_gh_failure(briefs, monkeypatch):
    (briefs / '1.txt').write_text(BRIEF)

    def boom(*a, **k):
        raise RuntimeError('gh api PATCH...: HTTP 502')
    monkeypatch.setattr(brief, '_gh', boom)
    t = make_tick({1: pr(1)}, dry=False)
    t.brief_pass()
    assert t.did == [] and 'failed to write the brief' in t.notes[0][1]
