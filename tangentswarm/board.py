"""Project 4 Status columns, matched by option id, never by name.

The board renames columns (Pending is now shown as Staging). Option ids stay the same,
so every reader maps a status value's optionId to the stable internal key below.
"""
PROJECT_ID = 'PVT_kwDOCSzmXs4BmmNr'
STATUS_FIELD = 'PVTSSF_lADOCSzmXs4BmmNrzhlOj8c'

# internal key -> option id
INBOX, TRIAGE, DISCUSS, BACKLOG, ACTIVE = 'Inbox', 'Triage', 'Discuss', 'Backlog', 'Active'
IN_PROGRESS, AUTO_REVIEW, NEEDS_INPUT = 'In Progress', 'Auto Review', 'Needs Input'
READY, STAGING, HUMAN_REVIEW = 'Ready', 'Pending', 'Human Review'  # STAGING keeps the internal key "Pending"
DONE, CANCELED, PARKED = 'Done', 'Canceled', 'Parked'

OPTION = {
    INBOX: 'c45b1427', TRIAGE: 'd810494e', DISCUSS: '3eb5de64', BACKLOG: '949e361c', ACTIVE: 'b78fd213',
    IN_PROGRESS: '99f45c3f', AUTO_REVIEW: 'd5518338', NEEDS_INPUT: 'a98589f0', READY: '432e495b',
    STAGING: 'd0ab725e', HUMAN_REVIEW: '3dbcefec', DONE: 'ded16603', CANCELED: '4f9967fe', PARKED: 'e3fa7392',
}
BY_ID = {v: k for k, v in OPTION.items()}

# Timeline status-change events carry the column name as it was at the time.
ALIASES = {STAGING: {'Approved', 'Staging'}}


def column(value):
    """Internal key for a Status field value {name, optionId}; the name only if the id is unknown."""
    value = value or {}
    return BY_ID.get(value.get('optionId')) or value.get('name')


def event_names(*keys):
    """Every name a timeline event may use for these columns."""
    return set(keys).union(*(ALIASES.get(k, set()) for k in keys))
