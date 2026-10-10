"""Wire protocol between the cloud hub and its clients (port of scialect src/protocol.mts).

Each websocket message is one JSON object. A request carries an `id`, and the
matching reply echoes it. Events that the server pushes have no id and use kind
"event".

client to server kinds: list, use{chatId}, send{text}, status{chatId?}, latest,
                        ping, subscribe{channel}, swarm-status, register{workerType}
server to client kinds: ok, list{chats,active}, use{active}, status{chat},
                        latest{text}, pong, swarm-status{changes}, error{message}
events:                 hello{serverVersion}, chat-update{chat},
                        message{chatId,text}, swarm-status{changes}

A ChatRef is {"id", "label", "transport": "cloud"|"tmux", "status"?, "slug"?}.
For cloud chats, id and label are both the sidebar session name.
"""
import json
import uuid

DEFAULT_PORT = 5002        # standalone server or the orchestrator
CLOUD_PORT = 5003          # cloud server that owns the browser, behind the orchestrator
WS_PATH = '/ws'
SERVER_VERSION = '0.1.0'

REQUEST_KINDS = {'list', 'use', 'send', 'status', 'latest', 'ping', 'subscribe',
                 'swarm-status', 'register'}


def default_url():
    import os
    return os.environ.get('TANGENTSWARM_CLOUD_URL') or os.environ.get('SCIALECT_URL') \
        or f"ws://127.0.0.1:{DEFAULT_PORT}{WS_PATH}"


def new_id():
    return str(uuid.uuid4())


def chat_ref(session_name, status=None, slug=None, transport='cloud'):
    ref = {'id': session_name, 'label': session_name, 'transport': transport}
    if status is not None:
        ref['status'] = status
    if slug is not None:
        ref['slug'] = slug
    return ref


def error(req_id, message):
    return {'id': req_id, 'kind': 'error', 'message': message}


def hello():
    return {'kind': 'event', 'type': 'hello', 'serverVersion': SERVER_VERSION}


def dumps(frame):
    return json.dumps(frame, ensure_ascii=False)
