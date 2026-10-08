"""swarm-mcp: an MCP server for driving tmux sessions, the coding agents
running in them, and Claude Code cloud sessions.

Transports
  stdio (default)   `swarm-mcp`; meant to be launched over SSH with a forced
                    command (the SSH key is the authentication).
  Streamable HTTP   `swarm-mcp --http [--host 127.0.0.1] [--port 8765]`; an
                    OAuth 2.0 resource server -- every request needs a bearer
                    token (see tangentswarm/auth.py and the README).
                    With `--auth-mode apikey` every request instead needs one
                    static API key (Authorization: Bearer / X-API-Key), checked
                    before any MCP handling (see tangentswarm/apikey.py).

Scopes (OAuth HTTP modes only; stdio is authenticated by SSH, and a valid API key
in apikey mode grants every scope)
  tangentswarm:read   list/capture/status tools  (required for every request)
  tangentswarm:shell  tools that type into panes or start sessions/windows

There is deliberately NO arbitrary-command tool (shell_exec was removed) and NO
kill-pane / kill-window / kill-session tools.
Nothing but MCP protocol traffic goes to stdout; logs go to stderr.
"""
import argparse
import functools
import inspect
import logging
import os
import sys

import anyio

from . import __version__, agents, tmux
from .auth import SCOPE_READ, SCOPE_SHELL

from mcp.server.mcpserver import MCPServer as _Server   # mcp 2.x (FastMCP was renamed)

INSTRUCTIONS = """Drive tmux on this host. Read with list_sessions/list_panes/capture_pane,
check agents with agent_status/pane_ready/wait_for_idle, type with send_keys or tell_agent
(literal text, then Enter after a 0.5s pause).
Targets use tmux syntax: session, session:window, session:window.pane or %pane_id.
cloud_* tools talk to Claude Code cloud sessions through the tangentswarm cloud hub
(`swarm cloud serve`, ws://127.0.0.1:5002/ws). There are no kill tools and no
arbitrary-command tool by design."""

# Set by run_http(); in stdio mode SSH has already authenticated the caller.
AUTH_ENFORCED = False
# True in apikey mode: ApiKeyMiddleware has already rejected every request without the key.
API_KEY_MODE = False

TOOL_SCOPES = {}   # tool name -> scope, for docs and tests


from mcp.server.mcpserver.exceptions import ToolError


class ScopeError(ToolError):
    pass


def require_scope(scope):
    """Enforce a scope when running behind OAuth (HTTP mode)."""
    if not AUTH_ENFORCED:
        return
    from mcp.server.auth.middleware.auth_context import get_access_token
    tok = get_access_token()
    if tok is None:
        raise ScopeError('authentication required')
    if scope not in tok.scopes:
        raise ScopeError(f'insufficient_scope: this tool requires the {scope} scope')


def _scoped(scope):
    """Decorator recording and enforcing a tool's scope (works for sync and async)."""
    def deco(fn):
        TOOL_SCOPES[fn.__name__] = scope
        # Errors are re-raised as ToolError so the client sees the real message
        # (the SDK masks any other exception as "Error executing tool").
        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def aw(*a, **kw):
                require_scope(scope)
                try:
                    return await fn(*a, **kw)
                except ToolError:
                    raise
                except Exception as e:
                    raise ToolError(_message(e)) from e
            return aw

        @functools.wraps(fn)
        def w(*a, **kw):
            require_scope(scope)
            try:
                return fn(*a, **kw)
            except ToolError:
                raise
            except Exception as e:
                raise ToolError(_message(e)) from e
        return w
    return deco


def _message(e):
    if isinstance(e, tmux.TmuxError):
        return e.stderr or str(e)
    return f"{e}" if str(e) else type(e).__name__


def _err(e):
    if isinstance(e, tmux.TmuxError):
        return RuntimeError(e.stderr or str(e))
    return e


# ---------------------------------------------------------------------------
# tool implementations (module level so tests can call them directly)

@_scoped(SCOPE_READ)
def list_sessions() -> dict:
    """List tmux sessions: name, id, windows, attached (client count), created (epoch seconds)."""
    try:
        return {'sessions': tmux.list_sessions()}
    except tmux.TmuxError as e:
        raise _err(e)


@_scoped(SCOPE_READ)
def list_panes(target: str | None = None, all: bool = False) -> dict:
    """List panes. target: a session ('agents', all its windows) or window ('agents:1').
    all=true lists every pane on the server. Each pane has session, window_index,
    window_name, pane_index, pane_id, active, current_command, current_path, width, height."""
    try:
        return {'panes': tmux.list_panes(target=target, all=all or not target)}
    except tmux.TmuxError as e:
        raise _err(e)


@_scoped(SCOPE_READ)
def capture_pane(target: str, history_lines: int | None = None, escapes: bool = False) -> str:
    """Return the visible text of a pane, plus `history_lines` lines of scrollback when set.
    escapes=true keeps colour/attribute escape sequences (capture-pane -e)."""
    try:
        return tmux.capture_pane(target, history_lines=history_lines, escapes=escapes)
    except tmux.TmuxError as e:
        raise _err(e)


@_scoped(SCOPE_SHELL)
async def send_keys(target: str, text: str, enter: bool = True, literal: bool = True) -> dict:
    """Type into a pane. literal=true (default) sends the text verbatim with send-keys -l,
    then (if enter) waits 0.5s and presses Enter in a separate call. literal=false passes
    `text` as tmux key names (e.g. 'C-c', 'Escape', 'Up')."""
    def run():
        if literal:
            tmux.send_keys_literal(target, text, enter=enter)
        else:
            r = tmux.send_keys(target, text, enter=enter)
            if r.returncode != 0:
                raise RuntimeError(f"send-keys failed for {target}")
    try:
        await anyio.to_thread.run_sync(run)
    except tmux.TmuxError as e:
        raise _err(e)
    return {'target': target, 'sent_chars': len(text), 'enter': enter, 'literal': literal}


@_scoped(SCOPE_SHELL)
def new_session(name: str, cwd: str | None = None, command: str | None = None) -> dict:
    """Create a detached tmux session (optionally in cwd, running command). Returns its first pane."""
    try:
        return tmux.new_session(name, cwd=cwd, command=command)
    except tmux.TmuxError as e:
        raise _err(e)


@_scoped(SCOPE_SHELL)
def new_window(session: str, name: str | None = None, cwd: str | None = None,
               command: str | None = None) -> dict:
    """Create a window in an existing session at the next free index, without switching
    clients to it. Returns the new pane."""
    try:
        return tmux.new_window(session, name=name, cwd=cwd, command=command)
    except tmux.TmuxError as e:
        raise _err(e)


@_scoped(SCOPE_READ)
async def agent_status(target: str) -> dict:
    """Which coding agent (claude, codex, gemini, opencode...) runs in a pane, whether its input
    prompt is blank, its current command/path and the last screen lines. Read-only."""
    try:
        return await anyio.to_thread.run_sync(agents.agent_status, target)
    except tmux.TmuxError as e:
        raise _err(e)


@_scoped(SCOPE_READ)
async def pane_ready(target: str, probe: bool = False) -> dict:
    """Is the agent's input prompt empty? probe=true uses scialect's space probe (types a space,
    checks, then backspaces) to see past placeholder text; it needs the shell scope."""
    if probe:
        require_scope(SCOPE_SHELL)
    try:
        return await anyio.to_thread.run_sync(lambda: agents.pane_ready(target, probe=probe))
    except tmux.TmuxError as e:
        raise _err(e)


@_scoped(SCOPE_READ)
async def wait_for_idle(target: str, timeout_sec: float = 120, poll_ms: int = 1000,
                        settle_sec: float = 3) -> dict:
    """Wait (max 600s) until the agent in a pane looks idle: screen unchanged for settle_sec
    and, for known agents, the prompt is blank. Returns idle true/false and waited_sec."""
    timeout_sec = max(1.0, min(float(timeout_sec), 600.0))
    poll = max(0.25, min(poll_ms / 1000.0, 10.0))
    try:
        return await anyio.to_thread.run_sync(
            lambda: agents.wait_for_idle(target, timeout=timeout_sec, poll=poll, settle=settle_sec))
    except tmux.TmuxError as e:
        raise _err(e)


@_scoped(SCOPE_SHELL)
async def tell_agent(target: str, text: str, new_conversation: bool = False,
                     require_empty_prompt: bool = True) -> dict:
    """Send a message to a coding agent the way scialect's tell-worker does: make sure the prompt
    is empty (space probe; refuses if someone is typing), optionally '/new' + Enter + 10s wait,
    then type the text literally, wait 0.5s, press Enter."""
    try:
        return await anyio.to_thread.run_sync(lambda: agents.tell_agent(
            target, text, new_conversation=new_conversation, require_empty_prompt=require_empty_prompt))
    except agents.AgentNotReady as e:
        raise RuntimeError(str(e))
    except tmux.TmuxError as e:
        raise _err(e)


@_scoped(SCOPE_READ)
async def swarm_status(control_dir: str) -> dict:
    """scialect's local-status table for the workers in <control_dir>/workers.jsonl:
    id, agent, state, health (OK/STUCK/ERR), status."""
    from . import local_status
    rows = await anyio.to_thread.run_sync(lambda: local_status.collect_swarm_rows(control_dir))
    return {'workers': local_status.rows_as_dicts(rows)}


@_scoped(SCOPE_SHELL)
async def tell_worker(control_dir: str, worker: str, verb: str, arg: str | None = None) -> dict:
    """Run a scialect state-machine handoff (tell-worker) using <control_dir>/workers.jsonl and
    its git-committed rules/. verb: assigned, accept, plan-approved, adjust, unblocked, reject,
    rebase [branch], or for the manager: review|approve-task|unblock <worker>."""
    import io
    from .tell_worker import TellWorker, TellWorkerError
    buf = io.StringIO()
    args = [worker, verb] + ([arg] if arg else [])
    try:
        await anyio.to_thread.run_sync(lambda: TellWorker(control_dir, out=buf).run(*args))
    except TellWorkerError as e:
        return {'ok': False, 'error': str(e), 'log': buf.getvalue().splitlines()}
    return {'ok': True, 'log': buf.getvalue().splitlines()}


def _cloud():
    try:
        from .cloud import client
        import websockets  # noqa: F401
    except ImportError as e:
        raise RuntimeError("cloud tools need: pip install 'tangentswarm[cloud]'") from e
    return client


@_scoped(SCOPE_READ)
async def cloud_list_sessions() -> dict:
    """List Claude Code cloud sessions visible in the claude.ai/code sidebar (id = visible name,
    status, URL slug). Needs the cloud hub (`swarm cloud serve`) running."""
    return await _cloud().cloud_list_sessions()


@_scoped(SCOPE_SHELL)
async def cloud_send_message(session_id: str, text: str) -> dict:
    """Switch to a cloud session (its visible name from cloud_list_sessions) and send text.
    Returns once submitted; use cloud_wait_for_response to block for the reply."""
    return await _cloud().cloud_send_message(session_id, text)


@_scoped(SCOPE_READ)
async def cloud_get_latest_response(session_id: str) -> dict:
    """Text of the most recent message in a cloud session's transcript (any author, so it can
    echo your own message right after cloud_send_message)."""
    return await _cloud().cloud_get_latest_response(session_id)


@_scoped(SCOPE_SHELL)
async def cloud_wait_for_response(session_id: str, text: str | None = None, timeout_sec: int = 120,
                                  poll_ms: int = 1500) -> dict:
    """Optionally send text, then poll until the session leaves running/awaiting AND the last
    transcript message differs from before. timeout_sec default 120, max 600; poll_ms 250-10000.
    Returns status, text, settled (false on timeout) and elapsed_sec."""
    return await _cloud().cloud_wait_for_response(session_id, text, timeout_sec, poll_ms)


TOOLS = [list_sessions, list_panes, capture_pane, send_keys, new_session, new_window,
         agent_status, pane_ready, wait_for_idle, tell_agent, swarm_status, tell_worker,
         cloud_list_sessions, cloud_send_message, cloud_get_latest_response, cloud_wait_for_response]


def build_server(**kwargs):
    kwargs.setdefault('version', __version__)
    server = _Server(name='tangentswarm', instructions=INSTRUCTIONS, **kwargs)
    for fn in TOOLS:
        server.add_tool(fn, name=fn.__name__)
    return server


# ---------------------------------------------------------------------------
# HTTP mode

def build_http_app(host='127.0.0.1', port=8765, path='/mcp', overrides=None):
    """Build the ASGI app for Streamable HTTP (OAuth, or a static API key in mode 'apikey').
    Returns (app, config, provider)."""
    from urllib.parse import urlsplit

    from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
    from mcp.server.transport_security import TransportSecuritySettings

    from . import apikey as K
    from . import auth as A

    global AUTH_ENFORCED, API_KEY_MODE
    overrides = dict(overrides or {})
    api_key_file = overrides.pop('api_key_file', None)
    if not overrides.get('mode') and not os.environ.get('TANGENTSWARM_AUTH_MODE') \
            and (api_key_file or K.key_configured()):
        overrides['mode'] = 'apikey'       # configuring a key selects apikey mode
    cfg = A.load_auth_config(host, port, path, overrides)
    if api_key_file and cfg.mode != 'apikey':
        raise K.ApiKeyError(f'--api-key-file only applies to --auth-mode apikey (mode is {cfg.mode})')
    provider = None
    api_key = None
    if cfg.mode == 'apikey':
        api_key, cfg.api_key_source = K.load_api_key(api_key_file)   # refuses to start without one
        cfg.api_key_fingerprint = K.fingerprint(api_key)
        server = build_server()
        AUTH_ENFORCED = False       # the key gate below authenticates; a valid key has all scopes
        API_KEY_MODE = True
    elif cfg.mode == 'builtin':
        provider = A.BuiltinProvider(cfg)
        settings = AuthSettings(
            issuer_url=cfg.issuer_url, resource_server_url=cfg.resource_url,
            required_scopes=cfg.required_scopes, validate_token_resource=True,
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=A.ALL_SCOPES, default_scopes=A.ALL_SCOPES),
            revocation_options=RevocationOptions(enabled=True))
        server = build_server(auth=settings, auth_server_provider=provider)
    else:
        settings = AuthSettings(issuer_url=cfg.issuer_url, resource_server_url=cfg.resource_url,
                                required_scopes=cfg.required_scopes, validate_token_resource=False)
        server = build_server(auth=settings, token_verifier=A.ExternalTokenVerifier(cfg))
    if api_key is None:
        AUTH_ENFORCED = True
        API_KEY_MODE = False

    allowed_hosts = ['127.0.0.1:*', 'localhost:*', '[::1]:*']
    allowed_origins = ['http://127.0.0.1:*', 'http://localhost:*', 'http://[::1]:*']
    pub = urlsplit(cfg.resource_url)
    if pub.hostname not in ('127.0.0.1', 'localhost', '::1'):
        allowed_hosts += [pub.netloc, pub.hostname]
        allowed_origins += [f"{pub.scheme}://{pub.netloc}"]
    security = TransportSecuritySettings(enable_dns_rebinding_protection=True,
                                         allowed_hosts=allowed_hosts, allowed_origins=allowed_origins)
    app = server.streamable_http_app(streamable_http_path=path, host=host, transport_security=security)
    if provider is not None:
        # /token (adds client_credentials, delegating other grants to the SDK) and /login
        app.router.routes[0:0] = A.make_extra_routes(provider)
    if api_key is not None:
        # outermost layer: no key, no MCP (or anything else)
        app = K.ApiKeyMiddleware(app, api_key)
    return app, cfg, provider


def run_http(host, port, path, overrides):
    import uvicorn
    from .apikey import ApiKeyError
    try:
        app, cfg, _ = build_http_app(host, port, path, overrides)
    except ApiKeyError as e:
        print(f"[tangentswarm] refusing to start: {e}", file=sys.stderr, flush=True)
        sys.exit(2)
    if cfg.mode == 'apikey':
        detail = f"key={cfg.api_key_source} {cfg.api_key_fingerprint}"
    else:
        detail = f"issuer={cfg.issuer_url}"
    print(f"[tangentswarm] swarm-mcp HTTP on http://{host}:{port}{path} "
          f"(auth={cfg.mode}, {detail}, resource={cfg.resource_url})",
          file=sys.stderr, flush=True)
    uvicorn.run(app, host=host, port=port, log_level='info')


def main(argv=None):
    ap = argparse.ArgumentParser(prog='swarm-mcp', description='tangentswarm MCP server (stdio by default)')
    ap.add_argument('--version', action='version', version=f'tangentswarm {__version__}')
    ap.add_argument('--http', action='store_true',
                    help='serve Streamable HTTP (OAuth, or a static API key) instead of stdio')
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--port', type=int, default=8765)
    ap.add_argument('--path', default='/mcp')
    ap.add_argument('--public-url', help='externally visible URL of the MCP endpoint (resource id)')
    ap.add_argument('--auth-mode', choices=['builtin', 'external', 'apikey'])
    ap.add_argument('--api-key-file', help='apikey mode: file holding the key (mode 600); default '
                    '$TANGENTSWARM_API_KEY, $TANGENTSWARM_API_KEY_FILE, ~/.config/tangentswarm/api_key')
    ap.add_argument('--gen-api-key', nargs='?', const='', metavar='PATH',
                    help='write a new random API key to PATH (default ~/.config/tangentswarm/api_key), '
                         'mode 600, without printing it; then exit')
    ap.add_argument('--issuer', help='external authorization server issuer URL')
    ap.add_argument('--audience', help='expected token audience (default: the resource URL)')
    a = ap.parse_args(argv)

    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    if a.gen_api_key is not None:
        from . import apikey as K
        try:
            p = K.generate_key_file(a.gen_api_key or None)
        except K.ApiKeyError as e:
            sys.exit(f"swarm-mcp: {e}")
        print(f"wrote a new API key to {p} (mode 600, {K.fingerprint(K.load_api_key(p)[0])})",
              file=sys.stderr)
        return
    if a.http:
        run_http(a.host, a.port, a.path, {'resource_url': a.public_url, 'mode': a.auth_mode,
                                          'issuer_url': a.issuer, 'audience': a.audience,
                                          'api_key_file': a.api_key_file})
        return
    build_server().run('stdio')


if __name__ == '__main__':
    main()
