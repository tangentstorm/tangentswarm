"""swarm-agent: the MCP server coding agents use to talk back to their operator. It
exposes the hub queue (tangentswarm.queue.hub, normally `swarm-hub` on localhost) and
nothing else: no tmux, no filesystem, no shell.

It has two transports.

* stdio (`swarm-agent`): one process per agent, inside the agent's own environment.
* Streamable HTTP (`swarm-agent --http [--host 127.0.0.1] [--port 8768]
  [--path /mcp/agent]`): one shared server for every agent, on any machine, behind a
  TLS reverse proxy. Auth works like swarm-mcp's (see http_app.py), with its own
  credentials: an API key separate from swarm-mcp's ($TANGENTSWARM_AGENT_API_KEY,
  $TANGENTSWARM_AGENT_API_KEY_FILE, ~/.config/tangentswarm/agent_api_key), or OAuth
  tokens carrying the tangentswarm:agent scope, issued for this server's resource URL.
  Neither grants anything on swarm-mcp, and swarm-mcp's credentials grant nothing here.

One server serves many agents, so every tool call needs to know which agent is calling.
The agent name is bound to the MCP session (Mcp-Session-Id), from the first of:

1. register_agent(name), called once at the start of the session;
2. an X-Swarm-Agent: <name> request header (set it in the client's MCP config, so the
   name survives server restarts and session re-initialization);
3. send_message's `sender` argument (kept for stdio clients written before this).

A session cannot switch names once bound, so a chatty agent cannot accidentally speak
as another one. The name is self-declared: everyone holding a valid credential is
trusted to say who they are. Each send is logged to stderr with the name and the
authenticated principal (OAuth client id or "apikey"), so a misbehaving credential can be
traced and revoked.

This is deliberately separate from swarm-mcp (tangentswarm/mcp_server.py), which runs on
the host and drives tmux: that one controls agents, this one lets an agent talk back.
"""
import argparse
import os
import re
import sys
import threading
from collections import OrderedDict
from urllib.parse import urlsplit

import anyio

from . import __version__
from .queue import client

from mcp.server.mcpserver import Context
from mcp.server.mcpserver import MCPServer as _Server
from mcp.server.mcpserver.exceptions import ToolError

DEFAULT_PORT = 8768
DEFAULT_PATH = '/mcp/agent'
AGENT_HEADER = 'x-swarm-agent'
AGENT_NAME_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._@:/+-]{0,63}')
ENV_KEY = 'TANGENTSWARM_AGENT_API_KEY'
ENV_KEY_FILE = 'TANGENTSWARM_AGENT_API_KEY_FILE'
ENV_PUBLIC_URL = 'TANGENTSWARM_AGENT_PUBLIC_URL'
KEY_FILE_NAME = 'agent_api_key'

INSTRUCTIONS = """Talk to the humans and agents running this swarm through the hub's message
queue, so they can see your news without watching your terminal. Start by calling
register_agent with your name (for example '<repo>/<branch>' or your session name) unless
your client already sends an X-Swarm-Agent header. Then send_message(recipient, type,
body, context): type is status, blocker, question, or done; recipient is a person or agent
name; context is optional free text (repo/branch, PR URL). read_messages returns new
messages addressed to you and remembers how far you have read."""

# True in apikey mode (set by build_http_app): the key gate has authenticated the caller.
API_KEY_MODE = False


def log(msg):
    print(f"[swarm-agent] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# which agent is calling

class Identities:
    """Agent name per MCP session. Bounded, oldest first out: a session that falls out
    just has to register again, and the X-Swarm-Agent header re-binds automatically."""

    def __init__(self, max_sessions=10000):
        self.max_sessions = max_sessions
        self._lock = threading.Lock()
        self._names = OrderedDict()   # session key -> (agent, principal)

    def get(self, key):
        with self._lock:
            hit = self._names.get(key)
            if hit is not None:
                self._names.move_to_end(key)
            return hit

    def bind(self, key, agent, principal):
        """Bind key to agent. Returns the agent, or raises ToolError if the session
        already has another name or belongs to another principal."""
        with self._lock:
            hit = self._names.get(key)
            if hit is not None:
                bound, owner = hit
                if owner != principal:
                    raise ToolError('this MCP session belongs to another credential')
                if bound != agent:
                    raise ToolError(f'this MCP session is registered as {bound!r}, not {agent!r}; '
                                    'start a new session to use another name')
                self._names.move_to_end(key)
                return bound
            self._names[key] = (agent, principal)
            while len(self._names) > self.max_sessions:
                self._names.popitem(last=False)
            return agent

    def clear(self):
        with self._lock:
            self._names.clear()


IDENTITIES = Identities()


def check_name(name):
    if not isinstance(name, str) or not AGENT_NAME_RE.fullmatch(name):
        raise ToolError('agent name must be 1-64 characters: letters, digits and ._@:/+- '
                        '(starting with a letter or digit)')
    return name


def _request_context(ctx):
    try:
        return ctx.request_context
    except (AttributeError, ValueError, LookupError):
        return None      # called outside a request, e.g. server.call_tool in tests


def _headers(ctx):
    rc = _request_context(ctx)
    return getattr(getattr(rc, 'request', None), 'headers', None) if rc is not None else None


def session_key(ctx):
    """The MCP session id over HTTP. The SDK's session manager only hands a request to the
    session its Mcp-Session-Id header names (unknown ids get 404), so the header is the
    session. stdio has one session per process, so one key."""
    headers = _headers(ctx)
    sid = headers.get('mcp-session-id') if headers is not None else None
    if not sid:
        rc = _request_context(ctx)
        sid = getattr(getattr(getattr(rc, 'session', None), '_connection', None), 'session_id', None)
    if sid:
        return f'http:{sid}'
    # an HTTP request outside any session (stateless) must never share stdio's key
    return None if headers is not None else 'stdio'


def principal():
    """Who authenticated this request: an OAuth client id, 'apikey', or 'local' (stdio)."""
    if API_KEY_MODE:
        return 'apikey'
    from mcp.server.auth.middleware.auth_context import get_access_token
    tok = get_access_token()
    return f'oauth:{tok.client_id}' if tok is not None else 'local'


def _header(ctx):
    headers = _headers(ctx)
    return headers.get(AGENT_HEADER) if headers is not None else None


def current_agent(ctx, claimed=None, required=True):
    """Resolve (and bind) the calling agent's name for this session."""
    key, who = session_key(ctx), principal()
    if key is None:
        # no session to remember a name in: the request itself has to say who it is
        names = {check_name(c.strip()) for c in (_header(ctx), claimed) if c}
        if len(names) > 1:
            raise ToolError('X-Swarm-Agent header and sender disagree')
        if not names and required:
            raise ToolError('unknown agent: without an MCP session, send an X-Swarm-Agent header')
        return names.pop() if names else None
    hit = IDENTITIES.get(key)
    if hit is not None and hit[1] != who:
        raise ToolError('this MCP session belongs to another credential')
    agent = hit[0] if hit else None
    for cand in (_header(ctx), claimed):
        if cand:
            agent = IDENTITIES.bind(key, check_name(cand.strip()), who)
    if agent is None and required:
        raise ToolError('unknown agent: call register_agent(name) first, '
                        'or send an X-Swarm-Agent header')
    return agent


# ---------------------------------------------------------------------------
# tools

async def register_agent(name: str, ctx: Context) -> dict:
    """Say which agent you are, once per session (for example 'tangentswarm/fix-login' or
    your session name). Messages you send are signed with it and read_messages returns
    messages addressed to it. A session cannot change its name later."""
    agent = current_agent(ctx, claimed=name)
    return {'agent': agent, 'principal': principal()}


async def whoami(ctx: Context) -> dict:
    """Return the agent name bound to this session (null if none yet) and the credential
    it authenticated with."""
    return {'agent': current_agent(ctx, required=False), 'principal': principal()}


async def send_message(recipient: str, type: str, body: str, ctx: Context,
                       context: str | None = None, sender: str | None = None) -> dict:
    """Send a message to the hub. type must be status, blocker, question, or done.
    The sender is the name this session registered; `sender` is only needed by clients
    that never call register_agent, and must match the registered name if both are set."""
    agent = current_agent(ctx, claimed=sender)
    try:
        res = await anyio.to_thread.run_sync(
            lambda: client.send_message(agent, recipient, type, body, context))
    except client.HubError as e:
        raise ToolError(str(e)) from e
    log(f"send id={res.get('id')} from={agent} to={recipient} type={type} principal={principal()}")
    return res


async def read_messages(ctx: Context, since: int | None = None, limit: int = 50) -> dict:
    """Return messages addressed to you, oldest first. With no `since`, it returns what you
    have not read yet and moves your read cursor past it; pass since=0 to re-read
    everything without moving the cursor."""
    agent = current_agent(ctx)
    reader = f'agent:{agent}'
    limit = max(1, min(int(limit), 200))

    def run():
        msgs = client.read_messages(reader, recipient=agent, since=since, limit=limit)
        cursor = None
        if since is None and msgs:
            cursor = client.advance_cursor(reader, msgs[-1]['id'])
        return msgs, cursor
    try:
        msgs, cursor = await anyio.to_thread.run_sync(run)
    except client.HubError as e:
        raise ToolError(str(e)) from e
    return {'agent': agent, 'messages': msgs, 'cursor': cursor}


TOOLS = [register_agent, whoami, send_message, read_messages]


def build_server(**kwargs):
    kwargs.setdefault('version', __version__)
    server = _Server(name='tangentswarm-agent', instructions=INSTRUCTIONS, **kwargs)
    for fn in TOOLS:
        server.add_tool(fn, name=fn.__name__)
    return server


# ---------------------------------------------------------------------------
# HTTP mode

def http_profile():
    from . import auth as A
    from . import paths
    from .http_app import HttpProfile
    return HttpProfile(name='swarm-agent', build_server=build_server,
                       required_scopes=[A.SCOPE_AGENT], registration_scopes=[A.SCOPE_AGENT],
                       key_env=ENV_KEY, key_file_env=ENV_KEY_FILE,
                       default_key_file=str(paths.config_dir() / KEY_FILE_NAME),
                       default_file_selects_apikey=True, external_checks_resource=True)


def build_http_app(host='127.0.0.1', port=DEFAULT_PORT, path=DEFAULT_PATH, overrides=None):
    """Build the ASGI app. Returns (app, config, provider).

    The resource URL is this server's own (--public-url, $TANGENTSWARM_AGENT_PUBLIC_URL, or
    http://host:port/path), never swarm-mcp's from the shared mcp-auth.yaml. In external
    mode, however it was selected (--auth-mode, $TANGENTSWARM_AUTH_MODE or mcp-auth.yaml),
    the issuer defaults to the origin of that URL, which is where a swarm-mcp running the
    builtin authorization server on the same host answers. An issuer set explicitly
    (--issuer, $TANGENTSWARM_AUTH_ISSUER or mcp-auth.yaml) wins."""
    from . import auth as A
    from .http_app import build_http_app as build
    from .http_app import configured, resolve_mode

    global API_KEY_MODE
    overrides = dict(overrides or {})
    resource = overrides.get('resource_url') or os.environ.get(ENV_PUBLIC_URL) \
        or A.local_origin(host, port) + path
    overrides['resource_url'] = resource
    overrides['audience'] = overrides.get('audience') or resource
    if resolve_mode(http_profile(), overrides) == 'external' and not (
            overrides.get('issuer_url') or os.environ.get('TANGENTSWARM_AUTH_ISSUER')
            or configured('issuer_url')):
        u = urlsplit(resource)
        overrides['issuer_url'] = f'{u.scheme}://{u.netloc}'
    app, cfg, provider = build(http_profile(), host, port, path, overrides)
    API_KEY_MODE = cfg.mode == 'apikey'
    return app, cfg, provider


def run_http(host, port, path, overrides):
    import uvicorn
    from .apikey import ApiKeyError
    from .http_app import describe
    try:
        app, cfg, _ = build_http_app(host, port, path, overrides)
    except ApiKeyError as e:
        log(f"refusing to start: {e}")
        sys.exit(2)
    log(f"HTTP on http://{host}:{port}{path} ({describe(cfg)}, resource={cfg.resource_url}, "
        f"hub={client._default_hub_url()})")
    uvicorn.run(app, host=host, port=port, log_level='info')


def main(argv=None):
    ap = argparse.ArgumentParser(prog='swarm-agent',
                                 description='Agent-side MCP server for the tangentswarm hub queue '
                                             '(stdio by default)')
    ap.add_argument('--version', action='version', version=f'tangentswarm {__version__}')
    ap.add_argument('--hub-url', help='hub to talk to (default $TANGENTSWARM_HUB_URL or '
                    f'{client.DEFAULT_HUB_URL})')
    ap.add_argument('--http', action='store_true',
                    help='serve Streamable HTTP for many agents instead of stdio')
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--port', type=int, default=DEFAULT_PORT)
    ap.add_argument('--path', default=DEFAULT_PATH)
    ap.add_argument('--public-url', help='externally visible URL of the MCP endpoint (resource id); '
                    f'default ${ENV_PUBLIC_URL} or http://host:port/path')
    ap.add_argument('--auth-mode', choices=['apikey', 'builtin', 'external'],
                    help='apikey: a static key; external: tokens from an issuer, by default a '
                         'swarm-mcp builtin authorization server at the public URL\'s origin; '
                         'builtin: be its own authorization server')
    ap.add_argument('--api-key-file', help='apikey mode: file holding the key (mode 600); default '
                    f'${ENV_KEY}, ${ENV_KEY_FILE}, ~/.config/tangentswarm/{KEY_FILE_NAME}')
    ap.add_argument('--gen-api-key', nargs='?', const='', metavar='PATH',
                    help=f'write a new random API key to PATH (default ~/.config/tangentswarm/'
                         f'{KEY_FILE_NAME}), mode 600, without printing it; then exit')
    ap.add_argument('--issuer', help='external mode: authorization server issuer URL')
    ap.add_argument('--audience', help='expected token audience (default: the resource URL)')
    a = ap.parse_args(argv)

    logging_to_stderr()
    if a.hub_url:
        os.environ['TANGENTSWARM_HUB_URL'] = a.hub_url
    if a.gen_api_key is not None:
        from . import apikey as K
        profile = http_profile()
        try:
            p = K.generate_key_file(a.gen_api_key or None, default_file=profile.default_key_file)
        except K.ApiKeyError as e:
            sys.exit(f"swarm-agent: {e}")
        print(f"wrote a new API key to {p} (mode 600, {K.fingerprint(K.load_api_key(p)[0])})",
              file=sys.stderr)
        return
    if a.http:
        run_http(a.host, a.port, a.path, {'resource_url': a.public_url, 'mode': a.auth_mode,
                                          'issuer_url': a.issuer, 'audience': a.audience,
                                          'api_key_file': a.api_key_file})
        return
    build_server().run('stdio')


def logging_to_stderr():
    import logging
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)


if __name__ == '__main__':
    main()
