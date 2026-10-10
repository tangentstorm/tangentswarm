"""In-memory swarm state with change detection (port of scialect src/swarm.mts).

The cloud orchestrator uses it to push `swarm-status` changes to subscribed
websocket clients.
"""
import sys
import time

from . import local_status

_current = {}


def get_current_swarm_state():
    return dict(_current)


def update_swarm_state(new_state):
    """Replace the state table and return only the workers that changed."""
    global _current
    changes = {wid: st for wid, st in new_state.items()
               if wid not in _current or any(_current[wid].get(k) != st.get(k)
                                             for k in ('state', 'status', 'agent', 'health'))}
    _current = dict(new_state)
    return changes


def rows_to_state(rows):
    """Rows are [id, agent, state, health, status].

    scialect's swarm.mts destructures rows as [id, agent, state, status], which picks
    up the health column as status. Here status is the real status column, and health
    is an extra field.
    """
    state = {}
    for row in rows:
        if not row:
            continue
        wid, agent, st, health, status = (list(row) + [''] * 5)[:5]
        state[wid] = {'agent': agent or '', 'state': st or '', 'status': status or '',
                      'health': health or ''}
    return state


def poll_swarm_once(cdir=None, emit=None, display=True, out=sys.stderr):
    rows = local_status.collect_swarm_rows(cdir)
    changes = update_swarm_state(rows_to_state(rows))
    if changes and emit:
        emit(changes)
    if display:
        print('\033[2J\033[H' + f"[swarm] {time.strftime('%H:%M:%S')}", file=out)
        print(local_status.format_swarm_table(rows), file=out, flush=True)
    return changes
