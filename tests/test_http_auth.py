"""Streamable HTTP + OAuth: run the real app under uvicorn on a free port."""
import base64
import hashlib
import json
import secrets
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import jwt
import pytest
import uvicorn
from cryptography.hazmat.primitives.asymmetric import rsa

from tangentswarm import auth as A
from tangentswarm import mcp_server

ACCEPT = 'application/json, text/event-stream'


def free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Server:
    def __init__(self, overrides=None):
        self.port = free_port()
        self.app, self.cfg, self.provider = mcp_server.build_http_app('127.0.0.1', self.port, '/mcp', overrides)
        self.base = f'http://127.0.0.1:{self.port}'
        self.server = uvicorn.Server(uvicorn.Config(self.app, host='127.0.0.1', port=self.port, log_level='error'))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        for _ in range(100):
            if self.server.started:
                return self
            time.sleep(0.05)
        raise RuntimeError('server did not start')

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(5)
        mcp_server.AUTH_ENFORCED = False


def http(method, url, body=None, headers=None, form=None):
    headers = dict(headers or {})
    data = None
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers.setdefault('Content-Type', 'application/x-www-form-urlencoded')
    elif body is not None:
        data = json.dumps(body).encode()
        headers.setdefault('Content-Type', 'application/json')
    req = urllib.request.Request(url, data=data, method=method, headers=headers)

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(req, timeout=10) as r:
            return r.status, dict(r.headers), r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode()


def rpc_messages(text):
    """Parse a JSON or SSE response body into JSON-RPC messages."""
    text = text.strip()
    if text.startswith('{') or text.startswith('['):
        return [json.loads(text)]
    return [json.loads(l[5:].strip()) for l in text.splitlines() if l.startswith('data:') and l[5:].strip()]


class Mcp:
    def __init__(self, srv, token):
        self.url = srv.base + '/mcp'
        self.token = token
        self.session = None
        self.n = 0

    def post(self, body):
        h = {'Accept': ACCEPT, 'MCP-Protocol-Version': '2025-06-18'}
        if self.token:
            h['Authorization'] = f'Bearer {self.token}'
        if self.session:
            h['Mcp-Session-Id'] = self.session
        status, headers, text = http('POST', self.url, body, h)
        sid = {k.lower(): v for k, v in headers.items()}.get('mcp-session-id')
        if sid:
            self.session = sid
        return status, headers, text

    def initialize(self):
        self.n += 1
        st, h, text = self.post({'jsonrpc': '2.0', 'id': self.n, 'method': 'initialize', 'params': {
            'protocolVersion': '2025-06-18', 'capabilities': {}, 'clientInfo': {'name': 't', 'version': '0'}}})
        if st == 200:
            self.post({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
        return st, h, text

    def call(self, method, params=None):
        self.n += 1
        st, h, text = self.post({'jsonrpc': '2.0', 'id': self.n, 'method': method, 'params': params or {}})
        assert st == 200, (st, text)
        return [m for m in rpc_messages(text) if m.get('id') == self.n][0]


# ---------------------------------------------------------------------------
# built-in authorization server

def test_unauthenticated_request_gets_401_with_resource_metadata():
    with Server() as srv:
        st, headers, _ = Mcp(srv, None).initialize()
        assert st == 401
        www = {k.lower(): v for k, v in headers.items()}['www-authenticate']
        assert www.startswith('Bearer') and 'resource_metadata=' in www
        st, _, body = http('GET', srv.base + '/.well-known/oauth-protected-resource/mcp')
        prm = json.loads(body)
        assert st == 200 and prm['resource'] == srv.base + '/mcp'
        assert prm['authorization_servers'][0].rstrip('/') == srv.base
        st, _, body = http('GET', srv.base + '/.well-known/oauth-authorization-server')
        meta = json.loads(body)
        assert meta['token_endpoint'] == srv.base + '/token' and 'S256' in meta['code_challenge_methods_supported']


def test_bad_token_is_401():
    with Server() as srv:
        assert Mcp(srv, 'tsw_not-a-real-token').initialize()[0] == 401


def test_preissued_token_and_scope_enforcement():
    store = A.AuthStore()
    full, _, _ = store.issue_token('memnar', A.ALL_SCOPES, 3600)
    ro, _, _ = store.issue_token('viewer', [A.SCOPE_READ], 3600)
    shell_only, _, _ = store.issue_token('odd', [A.SCOPE_SHELL], 3600)
    expired, _, _ = store.issue_token('old', A.ALL_SCOPES, 3600)
    store.x('UPDATE tokens SET expires_at=? WHERE client_id=?', (time.time() - 10, 'old'))
    revoked, rid, _ = store.issue_token('gone', A.ALL_SCOPES, 3600)
    store.revoke_token(token_id=rid)
    with Server() as srv:
        m = Mcp(srv, full)
        assert m.initialize()[0] == 200
        tools = [t['name'] for t in m.call('tools/list')['result']['tools']]
        assert 'shell_exec' in tools and not [t for t in tools if 'kill' in t]
        res = m.call('tools/call', {'name': 'shell_exec', 'arguments': {'command': 'echo hi', 'cwd': '/tmp'}})
        assert not res['result'].get('isError') and '"stdout": "hi\\n"' in res['result']['content'][0]['text']
        log = json.loads(open(A.paths.state_dir() / 'shell_exec.log').read().splitlines()[-1])
        assert log['principal']['client_id'] == 'memnar'

        r = Mcp(srv, ro)
        assert r.initialize()[0] == 200
        res = r.call('tools/call', {'name': 'shell_exec', 'arguments': {'command': 'echo no'}})
        assert res['result']['isError'] and 'insufficient_scope' in res['result']['content'][0]['text']
        res = r.call('tools/call', {'name': 'send_keys', 'arguments': {'target': 'x:0', 'text': 'no'}})
        assert res['result']['isError'] and 'tangentswarm:shell' in res['result']['content'][0]['text']

        assert Mcp(srv, shell_only).initialize()[0] == 403     # every request needs :read
        assert Mcp(srv, expired).initialize()[0] == 401
        assert Mcp(srv, revoked).initialize()[0] == 401


def test_client_credentials_grant():
    store = A.AuthStore()
    cid, secret = A.create_confidential_client(store, 'memnar', [A.SCOPE_READ])
    with Server() as srv:
        basic = base64.b64encode(f'{cid}:{secret}'.encode()).decode()
        st, _, body = http('POST', srv.base + '/token', form={'grant_type': 'client_credentials'},
                           headers={'Authorization': f'Basic {basic}'})
        assert st == 200, body
        tok = json.loads(body)
        assert tok['token_type'] == 'Bearer' and tok['scope'] == A.SCOPE_READ
        assert Mcp(srv, tok['access_token']).initialize()[0] == 200
        st, _, _ = http('POST', srv.base + '/token', form={'grant_type': 'client_credentials',
                                                          'client_id': cid, 'client_secret': 'wrong'})
        assert st == 401
        st, _, body = http('POST', srv.base + '/token', form={'grant_type': 'client_credentials', 'client_id': cid,
                                                             'client_secret': secret, 'scope': A.SCOPE_SHELL})
        assert st == 400 and 'invalid_scope' in body


def test_authorization_code_pkce_with_dcr_and_approval_code():
    store = A.AuthStore()
    approval = store.create_approval()
    with Server() as srv:
        redirect = 'http://127.0.0.1:9999/callback'
        st, _, body = http('POST', srv.base + '/register', body={
            'redirect_uris': [redirect], 'token_endpoint_auth_method': 'none',
            'grant_types': ['authorization_code', 'refresh_token'], 'response_types': ['code'],
            'client_name': 'skeletor'})
        assert st == 201, body
        client = json.loads(body)
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
        q = urllib.parse.urlencode({'response_type': 'code', 'client_id': client['client_id'],
                                    'redirect_uri': redirect, 'code_challenge': challenge,
                                    'code_challenge_method': 'S256', 'state': 'xyz',
                                    'scope': f'{A.SCOPE_READ} {A.SCOPE_SHELL}',
                                    'resource': srv.base + '/mcp'})
        st, h, _ = http('GET', srv.base + '/authorize?' + q)
        assert st == 302
        login_url = {k.lower(): v for k, v in h.items()}['location']
        req_id = urllib.parse.parse_qs(urllib.parse.urlsplit(login_url).query)['req'][0]
        st, _, page = http('GET', login_url)
        assert st == 200 and 'Approve' in page
        st, _, _ = http('POST', srv.base + '/login', form={'req': req_id, 'approval': 'wrong'})
        assert st == 401
        st, h, _ = http('POST', srv.base + '/login', form={'req': req_id, 'approval': approval})
        assert st == 302
        cb = urllib.parse.urlsplit({k.lower(): v for k, v in h.items()}['location'])
        params = urllib.parse.parse_qs(cb.query)
        assert params['state'] == ['xyz']
        st, _, body = http('POST', srv.base + '/token', form={
            'grant_type': 'authorization_code', 'code': params['code'][0], 'redirect_uri': redirect,
            'client_id': client['client_id'], 'code_verifier': verifier, 'resource': srv.base + '/mcp'})
        assert st == 200, body
        tok = json.loads(body)
        assert Mcp(srv, tok['access_token']).initialize()[0] == 200
        # refresh rotates
        st, _, body = http('POST', srv.base + '/token', form={
            'grant_type': 'refresh_token', 'refresh_token': tok['refresh_token'], 'client_id': client['client_id']})
        assert st == 200, body
        # revoke the new access token
        new = json.loads(body)['access_token']
        st, _, _ = http('POST', srv.base + '/revoke', form={'token': new, 'client_id': client['client_id'], 'client_secret': ''})
        assert st == 200
        assert Mcp(srv, new).initialize()[0] == 401


# ---------------------------------------------------------------------------
# external issuer (JWT via JWKS)

ISSUER = 'https://auth.example.test'


@pytest.fixture
def jwks_env(tmp_path, monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update({'kid': 'k1', 'use': 'sig', 'alg': 'RS256'})
    f = tmp_path / 'jwks.json'
    f.write_text(json.dumps({'keys': [jwk]}))
    monkeypatch.setenv('TANGENTSWARM_AUTH_MODE', 'external')
    monkeypatch.setenv('TANGENTSWARM_AUTH_ISSUER', ISSUER)
    monkeypatch.setenv('TANGENTSWARM_AUTH_JWKS_FILE', str(f))
    return key


def make_jwt(key, aud, scope, exp_delta=600, iss=ISSUER):
    now = int(time.time())
    return jwt.encode({'iss': iss, 'sub': 'memnar', 'aud': aud, 'scope': scope, 'iat': now,
                       'exp': now + exp_delta, 'client_id': 'memnar'}, key, algorithm='RS256',
                      headers={'kid': 'k1'})


def test_external_jwt_validation(jwks_env):
    key = jwks_env
    with Server() as srv:
        aud = srv.base + '/mcp'
        good = make_jwt(key, aud, f'{A.SCOPE_READ} {A.SCOPE_SHELL}')
        m = Mcp(srv, good)
        assert m.initialize()[0] == 200
        res = m.call('tools/call', {'name': 'list_sessions', 'arguments': {}})
        assert 'result' in res
        assert Mcp(srv, make_jwt(key, 'https://other.example/mcp', A.SCOPE_READ)).initialize()[0] == 401
        assert Mcp(srv, make_jwt(key, aud, A.SCOPE_READ, exp_delta=-3600)).initialize()[0] == 401
        assert Mcp(srv, make_jwt(key, aud, A.SCOPE_READ, iss='https://evil.test')).initialize()[0] == 401
        other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        assert Mcp(srv, make_jwt(other, aud, A.SCOPE_READ)).initialize()[0] == 401
        ro = Mcp(srv, make_jwt(key, aud, A.SCOPE_READ))
        assert ro.initialize()[0] == 200
        res = ro.call('tools/call', {'name': 'shell_exec', 'arguments': {'command': 'echo x'}})
        assert res['result']['isError'] and 'insufficient_scope' in res['result']['content'][0]['text']
        # PRM points at the external issuer
        prm = json.loads(http('GET', srv.base + '/.well-known/oauth-protected-resource/mcp')[2])
        assert prm['authorization_servers'][0].rstrip('/') == ISSUER


def test_external_mode_accepts_local_preissued_tokens(jwks_env):
    tok, _, _ = A.AuthStore().issue_token('memnar', A.ALL_SCOPES, 3600)
    with Server() as srv:
        assert Mcp(srv, tok).initialize()[0] == 200


def test_introspection_fallback(monkeypatch):
    cfg = A.AuthConfig(mode='external', issuer_url=ISSUER, resource_url='http://127.0.0.1:1/mcp',
                       audience='http://127.0.0.1:1/mcp', introspection_url='http://introspect.test/',
                       introspection_client_id='rs', introspection_client_secret='s3cret',
                       accept_local_tokens=False)
    seen = {}

    def fake_introspect(token):
        seen['token'] = token
        return {'active': token == 'opaque-good', 'iss': ISSUER, 'aud': cfg.audience,
                'scope': A.SCOPE_READ, 'exp': time.time() + 60, 'client_id': 'memnar'}
    v = A.ExternalTokenVerifier(cfg, store=None, jwks={'keys': []})
    monkeypatch.setattr(v, 'introspect', lambda t: fake_introspect(t) if fake_introspect(t)['active'] else None)
    at = v.verify_sync('opaque-good')
    assert at and at.client_id == 'memnar' and at.scopes == [A.SCOPE_READ]
    assert v.verify_sync('opaque-bad') is None
