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


def test_window_matches_pr_numbered_legacy_and_path():
    pr = {'number': 368, 'branch': 'ptw-attr-doc-tooltips', 'state': 'MERGED'}
    assert tick.window_matches_pr('ptw-368-ptw-attr-doc-tooltips', '/x', pr)
    assert tick.window_matches_pr('ptw-368-ptw-attr-doc-tooltips-b', '/x', pr)
    assert tick.window_matches_pr('ptw-attr-doc-tooltips', '/x', pr)
    assert tick.window_matches_pr('claude-ptw-attr-doc-tooltips', '/x', pr)
    assert tick.window_matches_pr('bash', '/home/memnar/ver/platform.ptw-attr-doc-tooltips (deleted)', pr)
    assert not tick.window_matches_pr('ptw-369-other', '/home/memnar/ver/platform.other', pr)
    slash = {'number': 398, 'branch': 'mn/avo-save-as', 'state': 'MERGED'}
    assert tick.window_matches_pr('mn-avo-save-as', '/x', slash)
    assert tick.window_matches_pr('mn-398-avo-save-as', '/x', slash)


def test_windows_to_close_skips_claude_node_dirty_and_open():
    merged = {396: {'number': 396, 'state': 'MERGED', 'branch': 'avo/outline-draft-saving'}}
    open_pr = {357: {'number': 357, 'state': 'OPEN', 'branch': 'outline-join-lines'}}
    numbered = {'id': '%107', 'window': 'avo-396-outline-draft-saving', 'command': 'bash',
                'claude': False, 'path': '/home/memnar/ver/platform.avo-outline-draft-saving (deleted)',
                'wt': None}
    legacy = {'id': '%101', 'window': 'ptw-attr-doc-tooltips', 'command': 'bash', 'claude': False,
              'path': '/home/memnar/ver/platform.ptw-attr-doc-tooltips',
              'wt': {'branch': 'ptw-attr-doc-tooltips', 'dirty': False, 'ahead': 0}}
    claude = dict(numbered, id='%99', window='avo-396-outline-draft-saving', command='claude', claude=True)
    node = dict(numbered, id='%80', command='node', claude=False)
    dirty = dict(legacy, id='%102', window='ptw-368-ptw-attr-doc-tooltips',
                 wt={'branch': 'ptw-attr-doc-tooltips', 'dirty': True, 'ahead': 0})
    open_legacy = {'id': '%76', 'window': 'outline-join-lines', 'command': 'bash', 'claude': False,
                   'path': '/home/memnar/ver/platform.outline-join-lines',
                   'wt': {'branch': 'outline-join-lines', 'dirty': False, 'ahead': 0}}
    prs = {**merged, 368: {'number': 368, 'state': 'MERGED', 'branch': 'ptw-attr-doc-tooltips'}, **open_pr}
    got = [(p['id'], pr['number']) for p, pr in tick.windows_to_close(
        [numbered, legacy, claude, node, dirty, open_legacy], prs)]
    assert got == [('%107', 396), ('%101', 368)]


def test_closable_closes_merged_shells_and_notes_finished_claude(monkeypatch):
    calls = []

    def fake_sh(argv, check=False, timeout=300):
        calls.append(list(argv))
        class R:
            returncode = 0
            stderr = stdout = ''
        return R()

    monkeypatch.setattr(tick, 'sh', fake_sh)
    merged = {'number': 396, 'state': 'MERGED', 'draft': False, 'branch': 'avo/outline-draft-saving',
              'head': HEAD, 'title': 't', 'col': 'Done', 'item': 'item', 'rank': 1, 'parked': False}
    shell = {'id': '%107', 'window': 'avo-396-outline-draft-saving', 'command': 'bash', 'claude': False,
             'cls': 'shell', 'pr': 396, 'parked': False, 'path': '/home/memnar/ver/platform.avo-outline-draft-saving (deleted)',
             'wt': None, 'screen': ''}
    finished = pane('finished')
    finished['pr'] = 396
    t = make_tick({396: merged, 7: dict(PR, col='Human Review', draft=False)}, [shell, finished], pr())
    t.dry = False
    t.closable()
    assert calls == [['tmux', 'kill-window', '-t', '%107']]
    assert t.did == ['did: closed window avo-396-outline-draft-saving (%107) for merged PR #396']
    assert any('Claude can be closed' in n[1] for n in t.notes)


def test_closable_dry_run_does_not_kill():
    merged = {'number': 396, 'state': 'MERGED', 'draft': False, 'branch': 'avo/outline-draft-saving',
              'head': HEAD, 'title': 't', 'col': 'Done', 'item': 'item', 'rank': 1, 'parked': False}
    shell = {'id': '%107', 'window': 'avo-396-outline-draft-saving', 'command': 'bash', 'claude': False,
             'cls': 'shell', 'pr': 396, 'parked': False,
             'path': '/home/memnar/ver/platform.avo-outline-draft-saving (deleted)', 'wt': None, 'screen': ''}
    t = make_tick({396: merged}, [shell], pr())
    t.closable()
    assert t.did == ['would: close window avo-396-outline-draft-saving (%107) for merged PR #396']
    assert t.notes == []


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


def test_typed_prompt_reads_input_box():
    s = screen(['● Done.'], prompt='merge origin/main into the branch')
    assert tick.typed_prompt(s) == 'merge origin/main into the branch'
    assert tick.typed_prompt(screen(['● Done.'], prompt='')) == ''


def test_attach_prs_skips_pane_with_no_worktree():
    prs = {7: dict(PR, draft=False)}
    panes = [
        {'id': '%1', 'window': 'code-7-b', 'wt': None, 'path': '/tmp/gone (deleted)'},
        {'id': '%2', 'window': 'other', 'wt': {'branch': 'b'}, 'path': '/x'},
    ]
    tick.attach_prs(panes, prs)
    assert panes[0]['pr'] == 7  # from the window name
    assert panes[1]['pr'] == 7  # from the worktree branch


def test_panes_pass_submits_typed_input():
    p = pane('input', screen=screen(['● Ready.'], prompt='merge origin/main into ptw/draw-at-playhead'))
    t = make_tick({7: dict(PR, draft=False)}, [p], pr())
    t.panes_pass(FakeScratch())
    assert t.notes == []
    assert t.did == ["would: #7 submit typed prompt in %9: merge origin/main into ptw/draw-at-playhead"]
