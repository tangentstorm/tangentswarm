"""Named tmux commands that act on a coding agent's own pane or window.

MCP clients never press tmux's prefix: send-keys skips tmux's key tables, so prefix keys
sent to a pane reach the agent as stray control characters. Instead, a client names an
action, and this module resolves the agent's pane and runs the matching tmux command
itself. The table is fixed. There is nothing that kills, respawns or runs a command.

Every action resolves `target` to the exact pane (the pane send-keys would hit) and
requires a registered agent in its foreground, then uses that pane's ids: %pane for
pane commands, @window for window commands and $session for indexes, so a renamed
session or window can't redirect the command.
"""
from . import agents, registry, tmux

TITLE_MAX = 64
INDEX_MAX = 9999


class ActionError(ValueError):
    """A bad action name or argument."""


def _where(pane_id):
    """The pane's session id, window id and index, and zoom/active flags."""
    fmt = '\t'.join(['#{session_id}', '#{window_id}', '#{window_index}',
                     '#{window_zoomed_flag}', '#{pane_active}'])
    sid, wid, index, zoomed, active = tmux.display(pane_id, fmt).split('\t')
    return {'session_id': sid, 'window_id': wid, 'window_index': int(index),
            'zoomed': zoomed == '1', 'active': active == '1'}


def _show_window(pane, w, arg):
    return [['select-window', '-t', w['window_id']]]


def _select_pane(pane, w, arg):
    return [['select-pane', '-t', pane]]


def _zoom(pane, w, arg):
    if w['zoomed'] and w['active']:
        return []                                   # already zoomed on this pane
    cmds = [['resize-pane', '-Z', '-t', pane]] if w['zoomed'] else []   # unzoom the other pane
    return cmds + [['resize-pane', '-Z', '-t', pane]]   # zooms and selects this pane


def _unzoom(pane, w, arg):
    return [['resize-pane', '-Z', '-t', pane]] if w['zoomed'] else []


def _rename_window(pane, w, arg):
    try:
        name = registry.validate_window_name(arg)
    except registry.AgentError as e:
        raise ActionError(str(e)) from None
    return [['rename-window', '-t', w['window_id'], name]]


def _move_window(pane, w, arg):
    try:
        index = int(arg)
    except (TypeError, ValueError):
        raise ActionError(f"move_window needs a window index, got {arg!r}") from None
    if isinstance(arg, bool) or not 0 <= index <= INDEX_MAX or str(index) != str(arg).strip():
        raise ActionError(f"window index must be an integer from 0 to {INDEX_MAX}, got {arg!r}")
    if index == w['window_index']:
        return []
    taken = {int(i) for i in tmux.run_checked(
        [tmux.TMUX, 'list-windows', '-t', w['session_id'], '-F', '#{window_index}']).stdout.split()}
    dest = f"{w['session_id']}:{index}"
    # -b on a taken index inserts the window there and moves the later windows up
    return [['move-window'] + (['-b'] if index in taken else []) + ['-s', w['window_id'], '-t', dest]]


def _set_pane_title(pane, w, arg):
    if not isinstance(arg, str) or not arg.strip() or len(arg) > TITLE_MAX or not arg.isprintable():
        raise ActionError(f"pane title must be 1 to {TITLE_MAX} printable characters, got {arg!r}")
    return [['select-pane', '-t', pane, '-T', arg]]


# name -> (builder, takes an argument?, description)
ACTIONS = {
    'show_window': (_show_window, False, "make the agent's window the current one in its session"),
    'select_pane': (_select_pane, False, "make the agent's pane the active one in its window"),
    'zoom': (_zoom, False, "zoom the agent's pane to fill its window"),
    'unzoom': (_unzoom, False, "restore the window's layout if it is zoomed"),
    'rename_window': (_rename_window, True, "rename the agent's window (arg: kebab-case, max 40)"),
    'move_window': (_move_window, True, "move the agent's window to index arg in its session; "
                                        "a taken index is inserted before, later windows move up"),
    'set_pane_title': (_set_pane_title, True, f"set the agent's pane title (arg: up to {TITLE_MAX} "
                                              "printable characters)"),
}


def run_action(target, action, arg=None):
    """Run a named action against the agent pane `target` resolves to. Returns what ran."""
    if action not in ACTIONS:
        raise ActionError(f"unknown action {action!r}; one of: {', '.join(ACTIONS)}")
    build, takes_arg, _ = ACTIONS[action]
    if takes_arg and arg is None:
        raise ActionError(f"{action} needs arg")
    if not takes_arg and arg is not None:
        raise ActionError(f"{action} takes no arg")
    info = agents.pane_agent(target)
    if not info['agent']:
        raise agents.NotAnAgentPane(
            f"{target} ({info['pane_id']}) is not running a registered coding agent "
            f"({', '.join(registry.AGENT_NAMES)}) in the foreground; refusing to act on it")
    pane = info['pane_id']
    cmds = build(pane, _where(pane), arg)
    for argv in cmds:
        tmux.run_checked([tmux.TMUX] + argv)
    return {'target': target, 'pane_id': pane, 'agent': info['agent'], 'action': action,
            'arg': arg, 'changed': bool(cmds), 'commands': [' '.join(c) for c in cmds]}
