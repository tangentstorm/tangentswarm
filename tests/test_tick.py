"""swarm-tick pure helpers: screen classes, PR matching, priority order, Codex state, gate reasons."""
from tangentswarm import tick

BAR = '─' * 60


def screen(body, prompt='', footer='  ⏵⏵ auto mode on (shift+tab to cycle)'):
    return '\n'.join([*body, BAR, f'❯ {prompt}', BAR, footer, ''])


def test_working_when_busy():
    s = screen(['✻ Thinking… (12s · esc to interrupt)'])
    assert tick.classify_screen(s, True) == 'working'


def test_menu_beats_blank_prompt():
    s = '\n'.join(['Do you want to proceed?', '❯ 1. Yes', '  2. No, and tell Claude what to do (esc)', '',
                   'Esc to cancel'])
    assert tick.classify_screen(s, True) == 'menu'
    assert tick.menu_line(s) == 'Do you want to proceed?'


def test_input_when_prompt_has_text():
    s = screen(['● Done.'], prompt='merge into work')
    assert tick.classify_screen(s, False) == 'input'


def test_asking_when_last_lines_end_in_question():
    s = screen(['● I pushed abc123.', '  Should I also merge it into work?'])
    assert tick.classify_screen(s, True) == 'asking'


def test_asking_when_question_is_earlier_in_the_message():
    s = screen(['● Status of #382.', '  Decision needed: add the force-ssl scope, or leave captions out?',
                '  Next steps:', '  - You: ask cornerbot to redeploy PTW staging.', '✻ Sautéed for 1m 48s'],
               prompt='add youtube.force-ssl')
    assert tick.classify_screen(s, True) == 'asking'
    assert tick.question_line(s).startswith('Decision needed')


def test_not_done_list_is_asking():
    s = screen(['● PR body rewritten.', '  Not done:', '  - Staging screenshots are still owed after the deploy.'])
    assert tick.classify_screen(s, True) == 'asking'


def test_old_question_before_the_last_message_does_not_count():
    s = screen(['● Should I push?', '● Pushed abc123 and updated the PR body.'])
    assert tick.classify_screen(s, True) == 'idle'


def test_idle_when_done():
    s = screen(['● Pushed 1234abcd. PR body updated.', '✻ Cooked for 3m 2s'])
    assert tick.classify_screen(s, True) == 'idle'


def test_screen_key_ignores_timer_and_spinner():
    a = screen(['● Running tests', '✻ Working… (1m 2s · esc to interrupt)'])
    b = screen(['● Running tests', '✻ Working… (9m 59s · esc to interrupt)'])
    c = screen(['● Running other tests', '✻ Working… (9m 59s · esc to interrupt)'])
    assert tick.screen_key(a) == tick.screen_key(b) != tick.screen_key(c)


def test_window_pr():
    assert tick.window_pr('code-382-avo-youtube-publish') == 382
    assert tick.window_pr('ptw-352-ptw-deck-countdown') == 352
    assert tick.window_pr('mn/avo-12-x') == 12
    assert tick.window_pr('claude-swarm-shells') is None
    assert tick.window_pr('outline-join-lines') is None


def test_milestone_rank():
    assert tick.milestone_rank(['PTW 15 ', None]) == 0
    assert tick.milestone_rank([None, 'ptw 16']) == 1
    assert tick.milestone_rank([]) == 1


def pr(head='a' * 40, comments=(), threads=(), reviews=(), committed='2026-11-11T10:00:00Z'):
    return {'headRefOid': head, 'comments': {'nodes': list(comments)},
            'reviewThreads': {'nodes': list(threads)}, 'reviews': {'nodes': list(reviews)},
            'commits': {'nodes': [{'commit': {'committedDate': committed}}]}}


CODEX = {'login': 'chatgpt-codex-connector'}


def test_codex_clean_receipt_on_head():
    body = "**Reviewed commit:** `aaaaaaaa`\nCodex Review: Didn't find any major issues."
    assert tick.codex_status(pr(comments=[{'author': CODEX, 'body': body, 'createdAt': 'x'}]))[0] == 'clean'


def test_codex_receipt_for_old_head_is_pending():
    body = "**Reviewed commit:** `bbbbbbbb`\nCodex Review: Didn't find any major issues."
    assert tick.codex_status(pr(comments=[{'author': CODEX, 'body': body, 'createdAt': 'x'}]))[0] == 'pending'


def test_codex_open_thread_is_findings():
    t = {'isResolved': False, 'isOutdated': False, 'comments': {'nodes': [{'author': CODEX}]}}
    body = "**Reviewed commit:** `aaaaaaaa`\nDidn't find any major issues"
    st, why = tick.codex_status(pr(threads=[t], comments=[{'author': CODEX, 'body': body, 'createdAt': 'x'}]))
    assert st == 'findings' and '1 open' in why


def test_codex_requested_only_after_head():
    before = {'author': {'login': 'tangentstorm'}, 'body': '@codex review', 'createdAt': '2026-11-11T09:00:00Z'}
    after = dict(before, createdAt='2026-11-11T11:00:00Z')
    bot = dict(after, author={'login': 'minbot[bot]'})
    assert not tick.codex_requested(pr(comments=[before, bot]))
    assert tick.codex_requested(pr(comments=[before, after]))


def test_gate_reasons_skip_running_ci_and_report_the_rest():
    rows = [('1 not draft', True, 'ready'),
            ('4 MinBot pass on head', False, 'label never ET'),
            ('5 CI green (Linux)', False, 'Linux GitHub rs=IN_PROGRESS'),
            ('6 deployed + served', False, 'static LACKS head'),
            ('9 stack-clean', False, 'conflicts')]
    out = tick.gate_reasons(rows)
    assert len(out) == 2
    assert out[0].startswith('needs a MinBot pass') and out[1].startswith('staging redeploy needed')


def test_gate_reasons_report_failed_ci():
    out = tick.gate_reasons([('5 CI green (Linux)', False, 'Linux GitHub rs=FAILURE')])
    assert out and out[0].startswith('Linux CI not green')


# --- the pane pass, with hr-gate and git faked ---------------------------------

class FakeScratch:
    dir, main = '/x', 'main'

    def ensure_main(self):
        pass

    def ensure(self, *a):
        pass


def make_tick(prs, panes, full, merge_clean=True):
    t = tick.Tick.__new__(tick.Tick)
    t.dry, t.verbose, t.out, t.did, t.notes = True, False, print, [], []
    t.g = {'fetch': lambda n, refresh=False: full, 'merge_tree_clean': lambda d, a, b: merge_clean,
           'card': lambda pr: ('item', 'In Progress')}

    class S:
        def done(self, key):
            return False
    t.state = S()
    t.prs, t.panes = prs, panes
    return t


HEAD = 'a' * 40
PR = {'number': 7, 'state': 'OPEN', 'draft': True, 'branch': 'b', 'head': HEAD, 'title': 't',
      'col': 'In Progress', 'item': 'item', 'rank': 1, 'parked': False}


def pane(cls, claude=True, head=HEAD, dirty=False, ahead=0, screen=''):
    return {'id': '%9', 'window': 'code-7-b', 'claude': claude, 'cls': cls, 'pr': 7, 'parked': False,
            'screen': screen, 'wt': {'branch': 'b', 'head': head, 'dirty': dirty, 'ahead': ahead}}


def test_finished_draft_goes_ready_once():
    t = make_tick({7: dict(PR)}, [pane('finished')], pr())
    t.panes_pass(FakeScratch())
    assert t.did == ['would: #7 marked ready (Claude finished, head aaaaaaaa pushed)',
                     'would: #7 card In Progress -> Auto Review',
                     'would: #7 posted "@codex review" for head aaaaaaaa']
    assert t.notes == []


def test_finished_with_conflicts_is_judgment():
    t = make_tick({7: dict(PR)}, [pane('finished')], pr(), merge_clean=False)
    t.panes_pass(FakeScratch())
    assert t.did == [] and 'conflicts with origin/main' in t.notes[0][1]


def test_finished_with_codex_findings_is_judgment(monkeypatch):
    monkeypatch.setattr(tick, 'mn_relayed', lambda n, h: False)
    thr = {'isResolved': False, 'isOutdated': False, 'comments': {'nodes': [{'author': CODEX}]}}
    t = make_tick({7: dict(PR)}, [pane('finished')], pr(threads=[thr]))
    t.panes_pass(FakeScratch())
    assert t.did == [] and 'Codex findings to relay' in t.notes[0][1]


def test_menu_and_dirty_and_exited_are_judgment():
    menu = '\n'.join(['Do you want to proceed?', '❯ 1. Yes', '2. No'])
    for p, want in [(pane('menu', screen=menu), 'at a menu: Do you want to proceed?'),
                    (pane('idle-dirty', dirty=True), 'idle with uncommitted changes'),
                    (pane('shell', claude=False, dirty=True), 'Claude exited mid-work')]:
        t = make_tick({7: dict(PR)}, [p], pr())
        t.panes_pass(FakeScratch())
        assert t.did == [] and want in t.notes[0][1], (want, t.notes)


def test_parked_pr_is_left_alone():
    t = make_tick({7: dict(PR, parked=True)}, [pane('finished')], pr())
    t.panes_pass(FakeScratch())
    assert t.did == [] and t.notes == []
