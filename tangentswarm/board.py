"""Project 4 Status columns, matched by option id, never by name.

The board renames columns (Pending is now shown as Staging). Option ids stay the same,
so every reader maps a status value's optionId to the stable internal key below.
"""
import json
import os
import re
from pathlib import Path

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


# Ordering (methodology#60): keep the flow full first; within that, a fast-track PR jumps
# the queue at every step. A PR is fast-track when it, or an issue it closes, has one of
# these labels.
FAST_TRACK_RE = re.compile(r'^\s*(fast[- _]?track|high[- _]?priority|priority[: /_-]*high)\s*$', re.I)
FAST_TRACK, MILESTONE, NORMAL = -1, 0, 1


def is_fast_track(labels):
    return any(FAST_TRACK_RE.match(l or '') for l in labels)


def priority_rank(fast_track, priority_milestone):
    """-1 fast-track, 0 priority milestone, 1 the rest. Lower goes first."""
    return FAST_TRACK if fast_track else (MILESTONE if priority_milestone else NORMAL)


# Only ONE fast-track ticket is active at a time (methodology PR #63). Several tickets may
# carry the label; the active one keeps the fast-track rank until it enters Human Review,
# is done or canceled, or loses the label. Then the candidate furthest along the board
# (lowest number on a tie) becomes active. A ticket is an issue or a PR: a PR counts as
# the issues it closes and itself. The swarm-promoted daemon keeps FT_STATE current; the
# other readers (swarm-tick, swarm-flow, hr-gate) read it with active_fast_track().
FT_STATE = Path(os.environ.get('SWARM_FASTTRACK_STATE',
                               str(Path.home() / '.local' / 'state' / 'swarm-fasttrack.json')))
FT_STAGES = (INBOX, TRIAGE, DISCUSS, BACKLOG, ACTIVE, IN_PROGRESS, NEEDS_INPUT, AUTO_REVIEW, READY, STAGING)


def ft_candidates(items):
    """{ticket number: stage index} for fast-track tickets that may be active.
    items: [{number, kind ('Issue'|'PullRequest'), state, col, labels, closes: [{number, labels}]}]."""
    out = {}
    for it in items:
        if it.get('state') != 'OPEN' or it.get('col') not in FT_STAGES:
            continue
        stage = FT_STAGES.index(it['col'])
        tickets = [it['number']] if is_fast_track(it.get('labels') or ()) else []
        tickets += [c['number'] for c in it.get('closes') or () if is_fast_track(c.get('labels') or ())]
        for t in tickets:
            out[t] = max(out.get(t, -1), stage)
    return out


def pick_active(cands, prev):
    """prev while it is still a candidate; else the furthest stage, then the lowest number."""
    if prev in cands:
        return prev
    if not cands:
        return None
    return min(cands, key=lambda t: (-cands[t], t))


def active_fast_track(path=None):
    """The active fast-track ticket number from FT_STATE, or None."""
    try:
        v = json.loads((path or FT_STATE).read_text()).get('active')
        return int(v) if v is not None else None
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def update_active(items, path=None):
    """Recompute and save the active fast-track ticket. Returns (active, previous)."""
    path = path or FT_STATE
    prev = active_fast_track(path)
    cands = ft_candidates(items)
    act = pick_active(cands, prev)
    if act != prev or not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix('.tmp')
        tmp.write_text(json.dumps({'active': act, 'candidates': sorted(cands)}))
        os.replace(tmp, path)
    return act, prev


def is_active_fast_track(number, closes=(), active=None):
    """True if PR/issue `number`, or an issue it closes, is the active fast-track ticket."""
    return active is not None and (number == active or active in set(closes))
