"""Board columns are matched by option id, so a renamed column keeps working."""
from tangentswarm import board, tick


def test_renamed_staging_column_maps_to_the_internal_key():
    assert board.column({'name': 'Staging', 'optionId': 'd0ab725e'}) == board.STAGING == 'Pending'
    assert board.column({'name': 'Anything new', 'optionId': '3dbcefec'}) == board.HUMAN_REVIEW


def test_unknown_id_falls_back_to_the_name_and_missing_value_is_none():
    assert board.column({'name': 'Brand new', 'optionId': 'ffffffff'}) == 'Brand new'
    assert board.column(None) is None


def test_event_names_include_old_and_new_names():
    assert board.event_names(board.STAGING) == {'Pending', 'Approved', 'Staging'}


def test_board_prs_uses_option_ids():
    item = {'id': 'I', 'fieldValueByName': {'name': 'Staging', 'optionId': 'd0ab725e'},
            'content': {'__typename': 'PullRequest', 'number': 5, 'state': 'OPEN', 'isDraft': False,
                        'headRefName': 'b', 'headRefOid': 'h', 'title': 't', 'body': '',
                        'repository': {'nameWithOwner': tick.REPO}, 'milestone': None,
                        'labels': {'nodes': []}, 'closingIssuesReferences': {'nodes': []}}}
    page = {'organization': {'projectV2': {'items': {'nodes': [item],
                                                     'pageInfo': {'hasNextPage': False, 'endCursor': None}}}}}
    prs = tick.board_prs({'gql': lambda q, c=None: page, 'PARKED': set()})
    assert prs[5]['col'] == 'Pending'


def test_fast_track_labels():
    for l in ('fast-track', 'Fast Track', 'fasttrack', 'high priority', 'High-Priority', 'priority: high'):
        assert board.is_fast_track([l]), l
    assert not board.is_fast_track(['swarm', 'priority-low', 'fast'])


def test_priority_rank_order():
    assert board.priority_rank(True, False) < board.priority_rank(False, True) < board.priority_rank(False, False)


def test_board_prs_fast_track_only_for_the_active_ticket(tmp_path):
    item = {'id': 'I', 'fieldValueByName': {'name': 'Ready', 'optionId': '432e495b'},
            'content': {'__typename': 'PullRequest', 'number': 9, 'state': 'OPEN', 'isDraft': False,
                        'headRefName': 'b', 'headRefOid': 'h', 'title': 't', 'body': '',
                        'repository': {'nameWithOwner': tick.REPO}, 'milestone': None,
                        'labels': {'nodes': [{'name': 'swarm'}]},
                        'closingIssuesReferences': {'nodes': [{'number': 488, 'milestone': None,
                                                               'labels': {'nodes': [{'name': 'fast-track'}]}}]}}}
    page = {'organization': {'projectV2': {'items': {'nodes': [item],
                                                     'pageInfo': {'hasNextPage': False, 'endCursor': None}}}}}
    g = {'gql': lambda q, c=None: page, 'PARKED': set()}
    assert tick.board_prs(g)[9]['rank'] == board.NORMAL  # labeled, but not the active one
    board.FT_STATE.write_text('{"active": 488}')
    assert tick.board_prs(g)[9]['rank'] == board.FAST_TRACK


def ft_item(n, col, labels=(), closes=(), kind='PullRequest', state='OPEN'):
    return dict(number=n, kind=kind, state=state, col=col, labels=list(labels),
                closes=[dict(number=c, labels=['fast-track']) for c in closes])


def test_only_one_fast_track_is_active_and_it_stays_active(tmp_path):
    path = tmp_path / 'ft.json'
    items = [ft_item(488, board.ACTIVE, ['fast-track'], kind='Issue'),
             ft_item(500, board.READY, ['high priority'])]
    assert board.update_active(items, path) == (500, None)  # furthest along goes first
    items.append(ft_item(501, board.STAGING, ['fast-track']))
    assert board.update_active(items, path) == (500, 500)   # sticky: a later one does not take over
    assert board.active_fast_track(path) == 500


def test_next_fast_track_when_the_active_enters_human_review_or_loses_the_label(tmp_path):
    path = tmp_path / 'ft.json'
    a = ft_item(500, board.READY, ['fast-track'])
    b = ft_item(490, board.IN_PROGRESS, closes=[488])  # a PR closing the labeled issue #488
    issue = ft_item(488, board.IN_PROGRESS, ['fast-track'], kind='Issue')
    assert board.update_active([a, b, issue], path)[0] == 500
    a['col'] = board.HUMAN_REVIEW
    assert board.update_active([a, b, issue], path)[0] == 488
    assert board.is_active_fast_track(490, closes=[488], active=488)
    assert not board.is_active_fast_track(500, active=488)
    b['closes'], issue['labels'] = [], []
    assert board.update_active([a, b, issue], path)[0] is None


def test_closed_or_canceled_tickets_are_not_candidates():
    assert board.ft_candidates([ft_item(1, board.READY, ['fast-track'], state='CLOSED'),
                                ft_item(2, board.CANCELED, ['fast-track'])]) == {}
