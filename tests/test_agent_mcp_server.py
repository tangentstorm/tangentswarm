"""Tests for swarm-agent, the agents' MCP server: tool dispatch over stdio-style calls,
then the shared Streamable HTTP server with many agents on one endpoint, its API key
(separate from swarm-mcp's), and OAuth tokens from swarm-mcp's builtin authorization
server. Everything runs for real: the hub over HTTP, uvicorn, the MCP SDK."""
import base64
import json
import os
import threading
import time

import anyio
import pytest
import uvicorn

from tangentswarm import agent_mcp_server as S
from tangentswarm import apikey as K
from tangentswarm import auth as A
from tangentswarm.queue import client
from tangentswarm.queue.hub import run_hub
from test_http_auth import Mcp, Server, free_port, http

AGENT_KEY = 'tsw_' + 'a' * 40
TMUX_KEY = 'tsw_' + 't' * 40


@pytest.fixture(autouse=True)
def fresh_identities(monkeypatch):
    for var in (S.ENV_KEY, S.ENV_KEY_FILE, S.ENV_PUBLIC_URL, 'TANGENTSWARM_HUB_URL'):
        monkeypatch.delenv(var, raising=False)
    S.IDENTITIES.clear()
    yield
    S.IDENTITIES.clear()
    S.API_KEY_MODE = False


@pytest.fixture
def hub_url(monkeypatch):
    ready = threading.Event()
    box = {}

    def on_ready(port):
        box['port'] = port
        ready.set()

    t = threading.Thread(target=run_hub, args=(':memory:',), kwargs={'port': 0, 'ready': on_ready},
                         daemon=True)
    t.start()
    assert ready.wait(5), 'hub did not start'
    url = f"http://127.0.0.1:{box['port']}"
    monkeypatch.setenv('TANGENTSWARM_HUB_URL', url)
    return url


def call(server, name, args):
    return anyio.run(server.call_tool, name, args)


# ---------------------------------------------------------------------------
# tools, called directly (one session, like stdio)

def test_tools_exposed():
    names = [t.name for t in anyio.run(S.build_server().list_tools)]
    assert names == ['register_agent', 'whoami', 'send_message', 'read_messages']


def test_send_message_with_sender_reaches_the_real_hub(hub_url):
    server = S.build_server()
    call(server, 'send_message', {'sender': 'agent-1', 'recipient': 'michal', 'type': 'done',
                                  'body': 'tests pass', 'context': None})
    msgs = client.read_messages('michal', since=0, hub_url=hub_url)
    assert [(m['sender'], m['type'], m['body']) for m in msgs] == [('agent-1', 'done', 'tests pass')]


def test_session_name_is_sticky_and_validated(hub_url):
    server = S.build_server()
    with pytest.raises(Exception, match='register_agent'):
        call(server, 'send_message', {'recipient': 'michal', 'type': 'status', 'body': 'x'})
    with pytest.raises(Exception, match='agent name'):
        call(server, 'register_agent', {'name': 'bad name; rm -rf'})
    call(server, 'register_agent', {'name': 'repo/branch'})
    call(server, 'register_agent', {'name': 'repo/branch'})          # idempotent
    with pytest.raises(Exception, match="registered as 'repo/branch'"):
        call(server, 'register_agent', {'name': 'someone-else'})
    with pytest.raises(Exception, match="registered as 'repo/branch'"):
        call(server, 'send_message', {'sender': 'someone-else', 'recipient': 'm', 'type': 'status',
                                      'body': 'spoof'})
    call(server, 'send_message', {'recipient': 'michal', 'type': 'status', 'body': 'hi'})
    assert client.read_messages('michal', since=0, hub_url=hub_url)[0]['sender'] == 'repo/branch'


def test_tool_error_on_unreachable_hub(monkeypatch):
    monkeypatch.setenv('TANGENTSWARM_HUB_URL', 'http://127.0.0.1:1')
    server = S.build_server()
    with pytest.raises(Exception, match='could not reach hub'):
        call(server, 'send_message', {'sender': 'agent-1', 'recipient': 'michal', 'type': 'status',
                                      'body': 'x'})


def test_identities_are_bounded():
    ids = S.Identities(max_sessions=2)
    for i in range(3):
        ids.bind(f'k{i}', f'a{i}', 'p')
    assert ids.get('k0') is None and ids.get('k2') == ('a2', 'p')
    with pytest.raises(Exception, match='another credential'):
        ids.bind('k2', 'a2', 'other')


# ---------------------------------------------------------------------------
# shared HTTP server

class AgentServer(Server):
    def __init__(self, overrides=None, port=None):
        self.port = port or free_port()
        self.app, self.cfg, self.provider = S.build_http_app('127.0.0.1', self.port, S.DEFAULT_PATH,
                                                             overrides)
        self.base = f'http://127.0.0.1:{self.port}'
        self.server = uvicorn.Server(uvicorn.Config(self.app, host='127.0.0.1', port=self.port,
                                                    log_level='error'))
        self.thread = threading.Thread(target=self.server.run, daemon=True)


class AgentMcp(Mcp):
    """An MCP client on /mcp/agent with an API key or bearer token and optional extra headers."""

    def __init__(self, srv, token, headers=None, key_header='bearer'):
        super().__init__(srv, None)
        self.url = srv.base + S.DEFAULT_PATH
        self.secret, self.extra, self.key_header = token, dict(headers or {}), key_header

    def post(self, body):
        h = {'Accept': 'application/json, text/event-stream', 'MCP-Protocol-Version': '2025-06-18',
             **self.extra}
        if self.secret is not None:
            if self.key_header == 'bearer':
                h['Authorization'] = f'Bearer {self.secret}'
            else:
                h['X-API-Key'] = self.secret
        if self.session:
            h['Mcp-Session-Id'] = self.session
        status, headers, text = http('POST', self.url, body, h)
        sid = {k.lower(): v for k, v in headers.items()}.get('mcp-session-id')
        if sid:
            self.session = sid
        return status, headers, text

    def tool(self, tool_name, **args):
        res = self.call('tools/call', {'name': tool_name, 'arguments': args})['result']
        text = res['content'][0]['text'] if res.get('content') else ''
        if res.get('isError'):
            raise RuntimeError(text)
        return res.get('structuredContent') or json.loads(text)


def key_file(tmp_path, key=AGENT_KEY, name='agent_api_key'):
    p = tmp_path / name
    p.write_text(key + '\n')
    os.chmod(p, 0o600)
    return p


def test_many_agents_share_one_endpoint(tmp_path, hub_url):
    with AgentServer({'api_key_file': str(key_file(tmp_path))}) as srv:
        assert srv.cfg.mode == 'apikey' and srv.cfg.resource_url == srv.base + '/mcp/agent'
        alice, bob = AgentMcp(srv, AGENT_KEY), AgentMcp(srv, AGENT_KEY, key_header='x-api-key')
        for m in (alice, bob):
            assert m.initialize()[0] == 200
        assert alice.session != bob.session
        assert alice.tool('whoami')['agent'] is None
        assert alice.tool('register_agent', name='alice')['principal'] == 'apikey'
        bob.tool('register_agent', name='bob')
        assert alice.tool('whoami')['agent'] == 'alice' and bob.tool('whoami')['agent'] == 'bob'

        alice.tool('send_message', recipient='bob', type='question', body='which port?')
        alice.tool('send_message', recipient='michal', type='status', body='working')
        bob.tool('send_message', recipient='alice', type='status', body='8768')
        with pytest.raises(RuntimeError, match="registered as 'bob'"):
            bob.tool('register_agent', name='alice')

        inbox = bob.tool('read_messages')
        assert [(m['sender'], m['body']) for m in inbox['messages']] == [('alice', 'which port?')]
        assert bob.tool('read_messages')['messages'] == []                  # cursor moved
        assert len(bob.tool('read_messages', since=0)['messages']) == 1     # re-read on demand
        assert [m['body'] for m in alice.tool('read_messages')['messages']] == ['8768']
        senders = {m['sender'] for m in client.read_messages('ops', since=0, hub_url=hub_url)}
        assert senders == {'alice', 'bob'}


def test_x_swarm_agent_header_binds_without_register(tmp_path, hub_url):
    with AgentServer({'api_key_file': str(key_file(tmp_path))}) as srv:
        m = AgentMcp(srv, AGENT_KEY, headers={'X-Swarm-Agent': 'carol'})
        assert m.initialize()[0] == 200
        m.tool('send_message', recipient='michal', type='done', body='merged')
        assert m.tool('whoami')['agent'] == 'carol'
        with pytest.raises(RuntimeError, match="registered as 'carol'"):
            m.tool('register_agent', name='dave')
        assert client.read_messages('x', recipient='michal', since=0, hub_url=hub_url)[0]['sender'] == 'carol'


def test_agent_key_is_separate_from_swarm_mcp_key(tmp_path, monkeypatch):
    # swarm-mcp's key, in any of its usual places, neither starts nor opens swarm-agent
    monkeypatch.setenv(K.ENV_KEY, TMUX_KEY)
    with pytest.raises(K.ApiKeyError, match='agent_api_key'):
        S.build_http_app('127.0.0.1', 1, S.DEFAULT_PATH, {'mode': 'apikey'})
    with AgentServer({'api_key_file': str(key_file(tmp_path))}) as srv:
        assert AgentMcp(srv, None).initialize()[0] == 401
        assert AgentMcp(srv, TMUX_KEY).initialize()[0] == 401
        assert AgentMcp(srv, AGENT_KEY).initialize()[0] == 200
    monkeypatch.setenv(S.ENV_KEY, AGENT_KEY)          # its own env var selects apikey mode
    with AgentServer() as srv:
        assert srv.cfg.mode == 'apikey' and AgentMcp(srv, AGENT_KEY).initialize()[0] == 200


def test_gen_api_key_defaults_to_agent_key_file(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv('TANGENTSWARM_CONFIG_DIR', str(tmp_path / 'cfg'))
    S.main(['--gen-api-key'])
    p = tmp_path / 'cfg' / 'agent_api_key'
    key, _ = K.load_api_key(p)
    assert oct(p.stat().st_mode & 0o777) == '0o600' and key not in capsys.readouterr().err
    assert not (tmp_path / 'cfg' / 'api_key').exists()
    # with no --auth-mode, the generated key file selects apikey mode
    _, cfg, _ = S.build_http_app('127.0.0.1', 1, S.DEFAULT_PATH)
    assert cfg.mode == 'apikey' and cfg.api_key_source == str(p)


def test_resource_url_ignores_swarm_mcp_config(monkeypatch):
    monkeypatch.setenv('TANGENTSWARM_AUTH_RESOURCE_URL', 'https://swarm.example/mcp')
    monkeypatch.setenv(S.ENV_PUBLIC_URL, 'https://swarm.example/mcp/agent')
    _, cfg, _ = S.build_http_app('127.0.0.1', 1, S.DEFAULT_PATH, {'mode': 'external'})
    assert cfg.resource_url == cfg.audience == 'https://swarm.example/mcp/agent'
    assert cfg.issuer_url == 'https://swarm.example'
    assert cfg.required_scopes == [A.SCOPE_AGENT]



@pytest.mark.parametrize('source', ['env', 'yaml'])
def test_external_mode_from_env_or_config_derives_issuer(source, monkeypatch):
    # external selected without --auth-mode still gets the issuer from the public URL's origin
    if source == 'env':
        monkeypatch.setenv('TANGENTSWARM_AUTH_MODE', 'external')
    else:
        A.config_file().parent.mkdir(parents=True, exist_ok=True)
        A.config_file().write_text('mode: external\n')
    _, cfg, _ = S.build_http_app('127.0.0.1', 1, S.DEFAULT_PATH,
                                 {'resource_url': 'https://swarm.example/mcp/agent'})
    assert cfg.mode == 'external' and cfg.issuer_url == 'https://swarm.example'


def test_explicit_issuer_from_env_or_config_wins(monkeypatch):
    A.config_file().parent.mkdir(parents=True, exist_ok=True)
    A.config_file().write_text('mode: external\nissuer_url: https://idp.example\n')
    _, cfg, _ = S.build_http_app('127.0.0.1', 1, S.DEFAULT_PATH,
                                 {'resource_url': 'https://swarm.example/mcp/agent'})
    assert cfg.issuer_url == 'https://idp.example'
    monkeypatch.setenv('TANGENTSWARM_AUTH_ISSUER', 'https://idp2.example')
    _, cfg, _ = S.build_http_app('127.0.0.1', 1, S.DEFAULT_PATH,
                                 {'resource_url': 'https://swarm.example/mcp/agent'})
    assert cfg.issuer_url == 'https://idp2.example'


# ---------------------------------------------------------------------------
# OAuth: swarm-mcp's builtin authorization server issues, swarm-agent accepts

def test_tokens_from_swarm_mcp_authorization_server(hub_url):
    store = A.AuthStore()
    cid, secret = A.create_confidential_client(store, 'cloud-agents', [A.SCOPE_AGENT])
    tmux_full, _, _ = store.issue_token('ops', A.ALL_SCOPES, 3600)
    preissued, _, _ = store.issue_token('agent-e', [A.SCOPE_AGENT], 3600)
    with Server() as tmux:
        agent_url = tmux.base + S.DEFAULT_PATH      # same origin, as behind the reverse proxy
        with AgentServer({'mode': 'external', 'resource_url': agent_url}) as srv:
            assert srv.cfg.issuer_url == tmux.base
            st, h, _ = AgentMcp(srv, None).initialize()
            www = {k.lower(): v for k, v in h.items()}['www-authenticate']
            assert st == 401 and '/.well-known/oauth-protected-resource/mcp/agent' in www
            prm = json.loads(http('GET', srv.base + '/.well-known/oauth-protected-resource/mcp/agent')[2])
            assert prm['resource'] == agent_url and prm['scopes_supported'] == [A.SCOPE_AGENT]
            assert prm['authorization_servers'][0].rstrip('/') == tmux.base

            basic = base64.b64encode(f'{cid}:{secret}'.encode()).decode()
            st, _, body = http('POST', tmux.base + '/token', headers={'Authorization': f'Basic {basic}'},
                               form={'grant_type': 'client_credentials', 'resource': agent_url})
            assert st == 200, body
            tok = json.loads(body)['access_token']
            m = AgentMcp(srv, tok)
            assert m.initialize()[0] == 200
            assert m.tool('register_agent', name='erin')['principal'] == f'oauth:{cid}'
            m.tool('send_message', recipient='michal', type='blocker', body='need a key')
            # the agent token opens nothing on swarm-mcp
            assert Mcp(tmux, tok).initialize()[0] == 401

            # a token for swarm-mcp's own resource is refused here, whatever its scopes
            st, _, body = http('POST', tmux.base + '/token', headers={'Authorization': f'Basic {basic}'},
                               form={'grant_type': 'client_credentials'})
            assert AgentMcp(srv, json.loads(body)['access_token']).initialize()[0] == 401
            # resources on other origins are not this authorization server's to grant
            st, _, body = http('POST', tmux.base + '/token', headers={'Authorization': f'Basic {basic}'},
                               form={'grant_type': 'client_credentials',
                                     'resource': 'https://elsewhere.example/mcp/agent'})
            assert st == 400 and 'invalid_target' in body

            assert AgentMcp(srv, tmux_full).initialize()[0] == 403      # no tangentswarm:agent
            assert AgentMcp(srv, preissued).initialize()[0] == 200      # `swarm auth token issue`
            assert Mcp(tmux, preissued).initialize()[0] == 403          # and it opens nothing there


def test_dcr_on_swarm_mcp_allows_the_agent_scope():
    with Server() as tmux:
        st, _, body = http('POST', tmux.base + '/register', body={
            'redirect_uris': ['http://127.0.0.1:9999/cb'], 'token_endpoint_auth_method': 'none',
            'grant_types': ['authorization_code', 'refresh_token'], 'response_types': ['code'],
            'client_name': 'claude', 'scope': A.SCOPE_AGENT})
        assert st == 201, body
        assert json.loads(body)['scope'] == A.SCOPE_AGENT


def test_builtin_mode_issues_only_the_agent_scope():
    with AgentServer({'mode': 'builtin'}) as srv:
        meta = json.loads(http('GET', srv.base + '/.well-known/oauth-authorization-server')[2])
        assert meta['scopes_supported'] == [A.SCOPE_AGENT]
        st, _, body = http('POST', srv.base + '/register', body={
            'redirect_uris': ['http://127.0.0.1:9999/cb'], 'token_endpoint_auth_method': 'none',
            'client_name': 'x', 'scope': A.SCOPE_SHELL})
        assert st == 400, body
