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


def test_board_prs_fast_track_from_a_closing_issue_label():
    item = {'id': 'I', 'fieldValueByName': {'name': 'Ready', 'optionId': '432e495b'},
            'content': {'__typename': 'PullRequest', 'number': 9, 'state': 'OPEN', 'isDraft': False,
                        'headRefName': 'b', 'headRefOid': 'h', 'title': 't', 'body': '',
                        'repository': {'nameWithOwner': tick.REPO}, 'milestone': None,
                        'labels': {'nodes': [{'name': 'swarm'}]},
                        'closingIssuesReferences': {'nodes': [{'milestone': None,
                                                               'labels': {'nodes': [{'name': 'fast-track'}]}}]}}}
    page = {'organization': {'projectV2': {'items': {'nodes': [item],
                                                     'pageInfo': {'hasNextPage': False, 'endCursor': None}}}}}
    assert tick.board_prs({'gql': lambda q, c=None: page, 'PARKED': set()})[9]['rank'] == board.FAST_TRACK
