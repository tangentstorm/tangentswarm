"""Thin HTTP client for the hub (tangentswarm.queue.hub), used by the
agent-side MCP server (tangentswarm/queue_mcp_server.py) and by anything
else -- a human's CLI, a bot -- that wants to read or write the queue.
"""
import json
import os
import urllib.error
import urllib.request
from urllib.parse import urlencode

DEFAULT_HUB_URL = 'http://127.0.0.1:8777'


class HubError(RuntimeError):
    pass


def _default_hub_url():
    # resolved per call, not baked into a function signature, so
    # $TANGENTSWARM_HUB_URL can be set (or monkeypatched) after import
    return os.environ.get('TANGENTSWARM_HUB_URL', DEFAULT_HUB_URL)


def _request(url, method='GET', payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                  headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read())['error']
        except Exception:
            msg = str(e)
        raise HubError(msg) from e
    except urllib.error.URLError as e:
        raise HubError(f"could not reach hub at {url}: {e}") from e


def send_message(sender, recipient, type, body, context=None, hub_url=None):
    """POST a message to the hub. type: status, blocker, question, or done."""
    hub_url = hub_url or _default_hub_url()
    return _request(f'{hub_url}/send', 'POST',
                     {'sender': sender, 'recipient': recipient, 'type': type,
                      'body': body, 'context': context})


def read_messages(reader, recipient=None, since=None, unread_only=False, limit=100,
                   hub_url=None):
    """Messages after `since` (default: reader's own cursor on the hub)."""
    hub_url = hub_url or _default_hub_url()
    qs = {'reader': reader, 'limit': limit}
    if recipient is not None:
        qs['recipient'] = recipient
    if since is not None:
        qs['since'] = since
    if unread_only:
        qs['unread_only'] = 'true'
    return _request(f'{hub_url}/read?{urlencode(qs)}')['messages']


def advance_cursor(reader, last_id, hub_url=None):
    hub_url = hub_url or _default_hub_url()
    return _request(f'{hub_url}/cursor', 'POST', {'reader': reader, 'last_id': last_id})['last_id']


def ack(message_id, hub_url=None):
    hub_url = hub_url or _default_hub_url()
    return _request(f'{hub_url}/ack', 'POST', {'id': message_id})['ok']
