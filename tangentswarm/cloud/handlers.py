"""Request dispatch for the cloud hub (port of scialect src/handlers.mts)."""
from . import protocol as P
from . import sessions as S
from .. import swarm_state


class ClientState:
    def __init__(self):
        self.active_chat = None
        self.subscriptions = set()


async def dispatch(deps, c, req, sessions=S):
    """deps is an object with .page, .with_active_chat(chat_id, coro_fn) and .broadcast(frame)."""
    kind = req.get('kind')
    rid = req.get('id', '?')
    if kind == 'ping':
        return {'id': rid, 'kind': 'pong'}
    if kind == 'list':
        chats = [P.chat_ref(s['name'], s['status'], s['slug']) for s in await sessions.list_sessions(deps.page)]
        return {'id': rid, 'kind': 'list', 'chats': chats, 'active': c.active_chat}
    if kind == 'use':
        match = next((s for s in await sessions.list_sessions(deps.page) if s['name'] == req.get('chatId')), None)
        if not match:
            return P.error(rid, f"no chat: {req.get('chatId')}")
        c.active_chat = match['name']
        return {'id': rid, 'kind': 'use', 'active': P.chat_ref(match['name'], match['status'], match['slug'])}
    if kind == 'status':
        chat_id = req.get('chatId') or c.active_chat
        if not chat_id:
            return P.error(rid, 'no active chat')
        status = await sessions.get_session_status(deps.page, chat_id)
        return {'id': rid, 'kind': 'status', 'chat': P.chat_ref(chat_id, status)}
    if kind == 'send':
        if not c.active_chat:
            return P.error(rid, 'no active chat')
        chat_id, text = c.active_chat, req.get('text', '')
        await deps.with_active_chat(chat_id, lambda: sessions.send_message(deps.page, text))
        await deps.broadcast({'kind': 'event', 'type': 'message', 'chatId': chat_id, 'text': text})
        return {'id': rid, 'kind': 'ok'}
    if kind == 'latest':
        if not c.active_chat:
            return P.error(rid, 'no active chat')
        text = await deps.with_active_chat(c.active_chat, lambda: sessions.get_latest_response(deps.page))
        return {'id': rid, 'kind': 'latest', 'text': text}
    if kind == 'subscribe':
        c.subscriptions.add(req.get('channel'))
        return {'id': rid, 'kind': 'ok'}
    if kind == 'swarm-status':
        return {'id': rid, 'kind': 'swarm-status', 'changes': swarm_state.get_current_swarm_state()}
    if kind == 'register':
        return P.error(rid, 'register must be handled by the hub')
    return P.error(rid, 'unknown request kind')
