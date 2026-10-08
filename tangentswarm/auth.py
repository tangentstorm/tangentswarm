"""OAuth 2.0 / OIDC auth for the Streamable HTTP mode of swarm-mcp.

The MCP server is an OAuth *resource server*.  It uses the official MCP SDK's
auth plumbing (BearerAuthBackend, RequireAuthMiddleware, Protected Resource
Metadata per RFC 9728, the authorization-server routes) and plugs in either:

* mode "external": a TokenVerifier for an outside issuer.  JWT access tokens are
  checked against the issuer's JWKS (signature, iss, aud/resource, exp, scope);
  opaque tokens fall back to RFC 7662 introspection when configured.  Tokens
  issued locally with `swarm auth token issue` are also accepted.

* mode "builtin": a small authorization server (OAuthAuthorizationServerProvider)
  backed by sqlite under ~/.local/state/tangentswarm/auth.db:
    - authorization code + PKCE with dynamic client registration (interactive
      clients); the authorize step is approved by the local admin password or a
      one-time approval code from `swarm auth approve`
    - client_credentials for confidential clients made with
      `swarm auth client add` (secret shown once, stored hashed)
    - pre-issued bearer tokens from `swarm auth token issue`
    - refresh tokens and RFC 7009 revocation

Scopes:  tangentswarm:read  is required for every request (list/capture/status);
         tangentswarm:shell is additionally required by tools that type into
         panes or start sessions/windows (send_keys, tell_agent, new_session, ...).

No secret is ever stored in the repo.  Tokens, client secrets and approval
codes are stored only as SHA-256 hashes; the admin password as a scrypt hash.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from html import escape
from typing import Any

import yaml

from . import paths

SCOPE_READ = 'tangentswarm:read'
SCOPE_SHELL = 'tangentswarm:shell'
ALL_SCOPES = [SCOPE_READ, SCOPE_SHELL]

TOKEN_PREFIX = 'tsw_'          # locally issued opaque tokens
JWT_ALGORITHMS = ['RS256', 'RS384', 'RS512', 'PS256', 'PS384', 'PS512',
                  'ES256', 'ES384', 'ES512', 'EdDSA']


# ---------------------------------------------------------------------------
# configuration

def config_file():
    return paths.config_dir() / 'mcp-auth.yaml'


def _env_list(name):
    val = os.environ.get(name)
    if val is None:
        return None
    return [s for s in val.replace(',', ' ').split() if s]


@dataclass
class AuthConfig:
    mode: str = 'builtin'                 # builtin | external | apikey (see apikey.py)
    resource_url: str = ''                # public URL of this MCP endpoint (…/mcp)
    issuer_url: str = ''                  # authorization server issuer
    audience: str = ''                    # expected aud/resource in tokens
    jwks_url: str | None = None
    jwks_file: str | None = None          # static JWKS (offline / tests)
    required_scopes: list[str] = field(default_factory=lambda: [SCOPE_READ])
    introspection_url: str | None = None
    introspection_client_id: str | None = None
    introspection_client_secret: str | None = None
    accept_local_tokens: bool = True
    access_token_ttl: int = 3600
    refresh_token_ttl: int = 30 * 24 * 3600
    leeway: int = 30

    def validate(self):
        if self.mode not in ('builtin', 'external', 'apikey'):
            raise ValueError(f"auth mode must be 'builtin', 'external' or 'apikey', not {self.mode!r}")
        if self.mode == 'external' and not self.issuer_url:
            raise ValueError('external auth mode needs issuer_url (TANGENTSWARM_AUTH_ISSUER)')
        return self


ENV_KEYS = {
    'mode': 'TANGENTSWARM_AUTH_MODE',
    'resource_url': 'TANGENTSWARM_AUTH_RESOURCE_URL',
    'issuer_url': 'TANGENTSWARM_AUTH_ISSUER',
    'audience': 'TANGENTSWARM_AUTH_AUDIENCE',
    'jwks_url': 'TANGENTSWARM_AUTH_JWKS_URL',
    'jwks_file': 'TANGENTSWARM_AUTH_JWKS_FILE',
    'introspection_url': 'TANGENTSWARM_AUTH_INTROSPECTION_URL',
    'introspection_client_id': 'TANGENTSWARM_AUTH_INTROSPECTION_CLIENT_ID',
    'introspection_client_secret': 'TANGENTSWARM_AUTH_INTROSPECTION_CLIENT_SECRET',
}


def load_auth_config(host='127.0.0.1', port=8765, path='/mcp', overrides=None):
    """Config precedence: defaults < ~/.config/tangentswarm/mcp-auth.yaml < env < overrides."""
    data: dict[str, Any] = {}
    cf = config_file()
    if cf.exists():
        data.update(yaml.safe_load(cf.read_text()) or {})
    for key, env in ENV_KEYS.items():
        if os.environ.get(env):
            data[key] = os.environ[env]
    secret_file = os.environ.get('TANGENTSWARM_AUTH_INTROSPECTION_CLIENT_SECRET_FILE') \
        or data.pop('introspection_client_secret_file', None)
    if secret_file and not data.get('introspection_client_secret'):
        data['introspection_client_secret'] = open(os.path.expanduser(secret_file)).read().strip()
    scopes = _env_list('TANGENTSWARM_AUTH_REQUIRED_SCOPES')
    if scopes is not None:
        data['required_scopes'] = scopes
    if os.environ.get('TANGENTSWARM_AUTH_ACCEPT_LOCAL_TOKENS'):
        data['accept_local_tokens'] = os.environ['TANGENTSWARM_AUTH_ACCEPT_LOCAL_TOKENS'].lower() in ('1', 'true', 'yes')
    data.update({k: v for k, v in (overrides or {}).items() if v is not None})

    known = {f for f in AuthConfig.__dataclass_fields__}
    cfg = AuthConfig(**{k: v for k, v in data.items() if k in known})
    base = f"http://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{port}"
    if not cfg.resource_url:
        cfg.resource_url = base + path
    if not cfg.issuer_url and cfg.mode == 'builtin':
        # the built-in AS lives at the origin of the resource URL
        u = urllib.parse.urlsplit(cfg.resource_url)
        cfg.issuer_url = f"{u.scheme}://{u.netloc}"
    if not cfg.audience:
        cfg.audience = cfg.resource_url
    if isinstance(cfg.required_scopes, str):
        cfg.required_scopes = cfg.required_scopes.split()
    return cfg.validate()


# ---------------------------------------------------------------------------
# hashing helpers (stdlib only; no hand-rolled crypto)

def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_secret(prefix=TOKEN_PREFIX, nbytes=32) -> str:
    return prefix + secrets.token_urlsafe(nbytes)


def hash_password(password: str) -> dict:
    salt = secrets.token_bytes(16)
    n, r, p = 2 ** 14, 8, 1
    dk = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=32)
    return {'alg': 'scrypt', 'n': n, 'r': r, 'p': p,
            'salt': base64.b64encode(salt).decode(), 'hash': base64.b64encode(dk).decode()}


def verify_password(password: str, rec: dict | None) -> bool:
    if not rec or rec.get('alg') != 'scrypt':
        return False
    dk = hashlib.scrypt(password.encode(), salt=base64.b64decode(rec['salt']),
                        n=rec['n'], r=rec['r'], p=rec['p'], dklen=32)
    return hmac.compare_digest(dk, base64.b64decode(rec['hash']))


def scopes_from_claims(claims: dict) -> list[str]:
    scope = claims.get('scope')
    if isinstance(scope, str):
        return scope.split()
    for key in ('scp', 'scopes'):
        val = claims.get(key)
        if isinstance(val, list):
            return [str(s) for s in val]
        if isinstance(val, str):
            return val.split()
    return []


# ---------------------------------------------------------------------------
# sqlite store

SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
  client_id TEXT PRIMARY KEY, info_json TEXT NOT NULL, secret_hash TEXT,
  kind TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS codes (
  code_hash TEXT PRIMARY KEY, data_json TEXT NOT NULL, expires_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS tokens (
  token_hash TEXT PRIMARY KEY, token_id TEXT NOT NULL, kind TEXT NOT NULL,
  client_id TEXT NOT NULL, scopes TEXT NOT NULL, expires_at REAL,
  resource TEXT, subject TEXT, label TEXT, created REAL NOT NULL,
  revoked INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS pending (
  req_id TEXT PRIMARY KEY, client_id TEXT NOT NULL, params_json TEXT NOT NULL,
  expires_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS approvals (
  code_hash TEXT PRIMARY KEY, expires_at REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0);
"""


class AuthStore:
    """Tiny sqlite store at ~/.local/state/tangentswarm/auth.db (mode 0600)."""

    def __init__(self, path=None):
        self.path = path or (paths.state_dir() / 'auth.db')
        paths.ensure_private_dir(os.path.dirname(self.path))
        new = not os.path.exists(self.path)
        if new:
            os.close(os.open(self.path, os.O_WRONLY | os.O_CREAT, 0o600))
        os.chmod(self.path, 0o600)
        self._lock = threading.Lock()
        self.db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def x(self, sql, args=()):
        with self._lock:
            return self.db.execute(sql, args).fetchall()

    # -- admin password -----------------------------------------------------
    @property
    def admin_file(self):
        return paths.state_dir() / 'admin.json'

    def set_admin_password(self, password):
        paths.write_private_file(self.admin_file, json.dumps(hash_password(password)))

    def check_admin_password(self, password):
        try:
            rec = json.loads(self.admin_file.read_text())
        except (OSError, ValueError):
            return False
        return verify_password(password, rec)

    def has_admin_password(self):
        return self.admin_file.exists()

    # -- one-time approval codes ---------------------------------------------
    def create_approval(self, ttl=600):
        code = secrets.token_urlsafe(9)
        self.x('INSERT INTO approvals VALUES (?,?,0)', (token_hash(code), time.time() + ttl))
        return code

    def consume_approval(self, code):
        h = token_hash(code.strip())
        rows = self.x('SELECT expires_at, used FROM approvals WHERE code_hash=?', (h,))
        if not rows or rows[0]['used'] or rows[0]['expires_at'] < time.time():
            return False
        self.x('UPDATE approvals SET used=1 WHERE code_hash=?', (h,))
        return True

    # -- clients -----------------------------------------------------------------
    def save_client(self, info: dict, kind: str, secret_hash=None):
        self.x('INSERT OR REPLACE INTO clients VALUES (?,?,?,?,?)',
               (info['client_id'], json.dumps(info), secret_hash, kind, time.time()))

    def get_client(self, client_id):
        rows = self.x('SELECT * FROM clients WHERE client_id=?', (client_id,))
        return dict(rows[0]) if rows else None

    def list_clients(self):
        return [dict(r) for r in self.x('SELECT client_id, kind, created, info_json FROM clients ORDER BY created')]

    def remove_client(self, client_id):
        self.x('DELETE FROM clients WHERE client_id=?', (client_id,))
        self.x('UPDATE tokens SET revoked=1 WHERE client_id=?', (client_id,))

    # -- tokens --------------------------------------------------------------------
    def issue_token(self, client_id, scopes, ttl, kind='access', resource=None,
                    subject=None, label=None):
        token = new_secret()
        token_id = 'tok_' + secrets.token_hex(6)
        expires = time.time() + ttl if ttl else None
        self.x('INSERT INTO tokens VALUES (?,?,?,?,?,?,?,?,?,?,0)',
               (token_hash(token), token_id, kind, client_id, ' '.join(scopes), expires,
                resource, subject, label, time.time()))
        return token, token_id, expires

    def lookup_token(self, token, kind=None):
        rows = self.x('SELECT * FROM tokens WHERE token_hash=?', (token_hash(token),))
        if not rows:
            return None
        row = dict(rows[0])
        if row['revoked'] or (kind and row['kind'] != kind):
            return None
        if row['expires_at'] is not None and row['expires_at'] < time.time():
            return None
        return row

    def revoke_token(self, token=None, token_id=None):
        if token is not None:
            self.x('UPDATE tokens SET revoked=1 WHERE token_hash=?', (token_hash(token),))
        if token_id is not None:
            self.x('UPDATE tokens SET revoked=1 WHERE token_id=?', (token_id,))

    def list_tokens(self, include_revoked=False):
        sql = ('SELECT token_id, kind, client_id, scopes, expires_at, label, created, revoked '
               'FROM tokens')
        if not include_revoked:
            sql += ' WHERE revoked=0'
        return [dict(r) for r in self.x(sql + ' ORDER BY created')]

    # -- codes / pending -------------------------------------------------------------
    def put_pending(self, client_id, params_json, ttl=600):
        req_id = secrets.token_urlsafe(24)
        self.x('INSERT INTO pending VALUES (?,?,?,?)', (req_id, client_id, params_json, time.time() + ttl))
        return req_id

    def pop_pending(self, req_id, consume=True):
        rows = self.x('SELECT * FROM pending WHERE req_id=?', (req_id,))
        if not rows or rows[0]['expires_at'] < time.time():
            return None
        if consume:
            self.x('DELETE FROM pending WHERE req_id=?', (req_id,))
        return dict(rows[0])

    def put_code(self, code, data_json, expires_at):
        self.x('INSERT INTO codes VALUES (?,?,?)', (token_hash(code), data_json, expires_at))

    def get_code(self, code):
        rows = self.x('SELECT * FROM codes WHERE code_hash=?', (token_hash(code),))
        return dict(rows[0]) if rows else None

    def delete_code(self, code):
        self.x('DELETE FROM codes WHERE code_hash=?', (token_hash(code),))


# ---------------------------------------------------------------------------
# built-in authorization server (SDK provider protocol)

def _access_token_from_row(token, row, default_resource=None):
    """Pre-issued tokens (swarm auth token issue) carry no resource; they are
    bound to this server, so they get the server's own resource URL."""
    from mcp.server.auth.provider import AccessToken
    return AccessToken(token=token, client_id=row['client_id'], scopes=row['scopes'].split(),
                       expires_at=int(row['expires_at']) if row['expires_at'] else None,
                       resource=row['resource'] or default_resource, subject=row['subject'],
                       claims={'token_id': row['token_id'], 'kind': row['kind']})


class BuiltinProvider:
    """Implements mcp.server.auth.provider.OAuthAuthorizationServerProvider."""

    def __init__(self, config: AuthConfig, store: AuthStore | None = None):
        self.config = config
        self.store = store or AuthStore()

    # clients
    async def get_client(self, client_id):
        from mcp.shared.auth import OAuthClientInformationFull
        row = self.store.get_client(client_id)
        if not row:
            return None
        return OAuthClientInformationFull.model_validate(json.loads(row['info_json']))

    async def register_client(self, client_info):
        info = client_info.model_dump(mode='json', exclude_none=True)
        # DCR clients: the SDK compares client_secret verbatim, so it is kept
        # (only) in the 0600 sqlite store.  CLI-made clients keep a hash only.
        self.store.save_client(info, kind='dcr')

    # authorization code + PKCE
    async def authorize(self, client, params):
        data = params.model_dump(mode='json')
        req_id = self.store.put_pending(client.client_id, json.dumps(data))
        return f"{self.config.issuer_url}/login?req={urllib.parse.quote(req_id)}"

    def complete_authorization(self, req_id, subject='admin'):
        """Called by the login page after the admin approved. Returns redirect URL."""
        from mcp.server.auth.provider import construct_redirect_uri
        pend = self.store.pop_pending(req_id)
        if not pend:
            raise ValueError('authorization request expired or unknown')
        p = json.loads(pend['params_json'])
        code = secrets.token_urlsafe(32)
        scopes = p.get('scopes') or list(ALL_SCOPES)
        data = {
            'code': code, 'scopes': scopes, 'expires_at': time.time() + 300,
            'client_id': pend['client_id'], 'code_challenge': p['code_challenge'],
            'redirect_uri': p['redirect_uri'],
            'redirect_uri_provided_explicitly': p['redirect_uri_provided_explicitly'],
            'resource': p.get('resource') or self.config.resource_url, 'subject': subject,
        }
        self.store.put_code(code, json.dumps(data), data['expires_at'])
        return construct_redirect_uri(p['redirect_uri'], code=code, state=p.get('state'))

    async def load_authorization_code(self, client, authorization_code):
        from mcp.server.auth.provider import AuthorizationCode
        row = self.store.get_code(authorization_code)
        if not row:
            return None
        data = json.loads(row['data_json'])
        if data['client_id'] != client.client_id:
            return None
        return AuthorizationCode(**data)

    def _issue_pair(self, client_id, scopes, resource, subject):
        from mcp.shared.auth import OAuthToken
        access, _, _ = self.store.issue_token(client_id, scopes, self.config.access_token_ttl,
                                              'access', resource, subject)
        refresh, _, _ = self.store.issue_token(client_id, scopes, self.config.refresh_token_ttl,
                                               'refresh', resource, subject)
        return OAuthToken(access_token=access, token_type='Bearer',
                          expires_in=self.config.access_token_ttl,
                          scope=' '.join(scopes), refresh_token=refresh)

    async def exchange_authorization_code(self, client, authorization_code):
        self.store.delete_code(authorization_code.code)   # single use
        return self._issue_pair(client.client_id, authorization_code.scopes,
                                authorization_code.resource or self.config.resource_url,
                                authorization_code.subject)

    async def load_refresh_token(self, client, refresh_token):
        from mcp.server.auth.provider import RefreshToken
        row = self.store.lookup_token(refresh_token, kind='refresh')
        if not row or row['client_id'] != client.client_id:
            return None
        return RefreshToken(token=refresh_token, client_id=row['client_id'],
                            scopes=row['scopes'].split(),
                            expires_at=int(row['expires_at']) if row['expires_at'] else None,
                            resource=row['resource'], subject=row['subject'])

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        self.store.revoke_token(token=refresh_token.token)   # rotate
        return self._issue_pair(client.client_id, scopes or refresh_token.scopes,
                                refresh_token.resource or self.config.resource_url,
                                refresh_token.subject)

    async def load_access_token(self, token):
        row = self.store.lookup_token(token, kind='access')
        return _access_token_from_row(token, row, self.config.resource_url) if row else None

    async def revoke_token(self, token):
        self.store.revoke_token(token=token.token)

    async def exchange_identity_assertion(self, client, params):  # pragma: no cover
        from mcp.server.auth.provider import TokenError
        raise TokenError('unsupported_grant_type', 'identity assertion grant is not supported')

    # client_credentials (not part of the SDK's token handler; see token_endpoint)
    def client_credentials(self, client_id, client_secret, requested_scopes):
        row = self.store.get_client(client_id)
        if not row or row['kind'] != 'confidential' or not row['secret_hash']:
            return None, 'invalid_client'
        if not hmac.compare_digest(row['secret_hash'], token_hash(client_secret or '')):
            return None, 'invalid_client'
        info = json.loads(row['info_json'])
        allowed = (info.get('scope') or '').split()
        scopes = requested_scopes or allowed
        if any(s not in allowed for s in scopes):
            return None, 'invalid_scope'
        token, _, _ = self.store.issue_token(client_id, scopes, self.config.access_token_ttl,
                                             'access', self.config.resource_url, client_id)
        return {'access_token': token, 'token_type': 'Bearer',
                'expires_in': self.config.access_token_ttl, 'scope': ' '.join(scopes)}, None


def create_confidential_client(store: AuthStore, name: str, scopes: list[str]):
    """Create a client for the client_credentials grant. Returns (client_id, secret)."""
    client_id = 'tsc_' + secrets.token_hex(8)
    secret = new_secret('tss_')
    info = {'client_id': client_id, 'client_name': name, 'scope': ' '.join(scopes),
            'grant_types': ['client_credentials'], 'token_endpoint_auth_method': 'client_secret_basic',
            'redirect_uris': None}
    store.save_client({k: v for k, v in info.items() if v is not None}, 'confidential', token_hash(secret))
    return client_id, secret


# ---------------------------------------------------------------------------
# external issuer verifier (JWT via JWKS, RFC 7662 introspection fallback)

class ExternalTokenVerifier:
    """mcp.server.auth.provider.TokenVerifier for an outside OAuth/OIDC issuer."""

    def __init__(self, config: AuthConfig, store: AuthStore | None = None, jwks: dict | None = None):
        self.config = config
        self.store = store if store is not None else (AuthStore() if config.accept_local_tokens else None)
        self._jwks = jwks
        if jwks is None and config.jwks_file:
            self._jwks = json.loads(open(os.path.expanduser(config.jwks_file)).read())
        self._jwk_client = None

    # -- JWKS --------------------------------------------------------------------------
    def _discover_jwks_url(self):
        if self.config.jwks_url:
            return self.config.jwks_url
        base = self.config.issuer_url.rstrip('/')
        for wk in ('/.well-known/openid-configuration', '/.well-known/oauth-authorization-server'):
            try:
                with urllib.request.urlopen(base + wk, timeout=10) as r:
                    meta = json.loads(r.read())
                if meta.get('jwks_uri'):
                    self.config.jwks_url = meta['jwks_uri']
                    return meta['jwks_uri']
            except (urllib.error.URLError, ValueError, OSError):
                continue
        raise RuntimeError(f'could not discover jwks_uri for issuer {self.config.issuer_url}')

    def _signing_key(self, token):
        import jwt
        if self._jwks is not None:
            header = jwt.get_unverified_header(token)
            keys = jwt.PyJWKSet.from_dict(self._jwks).keys
            kid = header.get('kid')
            for k in keys:
                if kid is None or k.key_id == kid:
                    return k.key
            raise jwt.InvalidTokenError('no matching JWK')
        if self._jwk_client is None:
            self._jwk_client = jwt.PyJWKClient(self._discover_jwks_url(), cache_keys=True)
        return self._jwk_client.get_signing_key_from_jwt(token).key

    def _audience_ok(self, claims):
        aud = claims.get('aud')
        auds = [aud] if isinstance(aud, str) else list(aud or [])
        want = {self.config.audience.rstrip('/'), self.config.resource_url.rstrip('/')}
        if any(str(a).rstrip('/') in want for a in auds):
            return True
        res = claims.get('resource')
        return isinstance(res, str) and res.rstrip('/') in want

    def verify_jwt(self, token):
        import jwt
        key = self._signing_key(token)
        claims = jwt.decode(token, key=key, algorithms=JWT_ALGORITHMS,
                            issuer=self.config.issuer_url, leeway=self.config.leeway,
                            options={'require': ['exp', 'iss'], 'verify_aud': False})
        if not self._audience_ok(claims):
            raise jwt.InvalidAudienceError('token audience/resource does not match this server')
        return claims

    def introspect(self, token):
        cfg = self.config
        body = urllib.parse.urlencode({'token': token, 'token_type_hint': 'access_token'}).encode()
        req = urllib.request.Request(cfg.introspection_url, data=body, method='POST',
                                     headers={'Content-Type': 'application/x-www-form-urlencoded',
                                              'Accept': 'application/json'})
        if cfg.introspection_client_id:
            cred = f"{urllib.parse.quote(cfg.introspection_client_id)}:" \
                   f"{urllib.parse.quote(cfg.introspection_client_secret or '')}"
            req.add_header('Authorization', 'Basic ' + base64.b64encode(cred.encode()).decode())
        with urllib.request.urlopen(req, timeout=10) as r:
            claims = json.loads(r.read())
        if not claims.get('active'):
            return None
        if claims.get('exp') is not None and claims['exp'] + cfg.leeway < time.time():
            return None
        if claims.get('iss') and claims['iss'].rstrip('/') != cfg.issuer_url.rstrip('/'):
            return None
        if (claims.get('aud') or claims.get('resource')) and not self._audience_ok(claims):
            return None
        return claims

    def _to_access_token(self, token, claims):
        from mcp.server.auth.provider import AccessToken
        return AccessToken(token=token,
                           client_id=str(claims.get('client_id') or claims.get('azp') or claims.get('sub') or 'unknown'),
                           scopes=scopes_from_claims(claims),
                           expires_at=int(claims['exp']) if claims.get('exp') else None,
                           resource=self.config.resource_url, subject=claims.get('sub'),
                           claims={'iss': claims.get('iss')})

    def verify_sync(self, token):
        import jwt
        if self.store is not None and token.startswith(TOKEN_PREFIX):
            row = self.store.lookup_token(token, kind='access')
            return _access_token_from_row(token, row, self.config.resource_url) if row else None
        if token.count('.') == 2:
            try:
                return self._to_access_token(token, self.verify_jwt(token))
            except jwt.PyJWTError:
                if not self.config.introspection_url:
                    return None
        if self.config.introspection_url:
            try:
                claims = self.introspect(token)
            except (urllib.error.URLError, ValueError, OSError):
                return None
            return self._to_access_token(token, claims) if claims else None
        return None

    async def verify_token(self, token):
        import anyio
        return await anyio.to_thread.run_sync(self.verify_sync, token)


# ---------------------------------------------------------------------------
# HTTP routes added next to the SDK's: /token (client_credentials) and /login

LOGIN_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>tangentswarm login</title>
<style>body{{font-family:sans-serif;max-width:32em;margin:4em auto}}input{{font-size:1.1em;width:100%;margin:.3em 0}}</style>
</head><body><h2>Authorize MCP client</h2>
<p>Client <b>{client}</b> is asking for: <code>{scopes}</code></p>
<p>{error}</p>
<form method="post"><input type="hidden" name="req" value="{req}">
<label>Admin password<input type="password" name="password" autocomplete="current-password"></label>
<label>or one-time approval code (<code>swarm auth approve</code>)<input type="text" name="approval"></label>
<input type="submit" value="Approve"></form></body></html>"""


def make_extra_routes(provider: BuiltinProvider):
    """Starlette routes for the built-in AS that the SDK does not provide."""
    from mcp.server.auth.handlers.token import TokenHandler
    from mcp.server.auth.middleware.client_auth import ClientAuthenticator
    from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
    from starlette.routing import Route

    sdk_token = TokenHandler(provider, ClientAuthenticator(provider))

    def _err(code, desc, status=400):
        return JSONResponse({'error': code, 'error_description': desc}, status_code=status,
                            headers={'Cache-Control': 'no-store'})

    async def token_endpoint(request):
        form = await request.form()   # cached on the Request, so the SDK handler can re-read it
        if form.get('grant_type') != 'client_credentials':
            return await sdk_token.handle(request)
        client_id, secret = form.get('client_id'), form.get('client_secret')
        auth = request.headers.get('authorization', '')
        if auth.startswith('Basic '):
            try:
                cid, sec = base64.b64decode(auth[6:]).decode().split(':', 1)
                client_id, secret = urllib.parse.unquote(cid), urllib.parse.unquote(sec)
            except (ValueError, UnicodeDecodeError):
                return _err('invalid_client', 'bad Basic auth header', 401)
        if not client_id:
            return _err('invalid_client', 'missing client credentials', 401)
        scopes = (form.get('scope') or '').split()
        result, error = provider.client_credentials(str(client_id), str(secret or ''), scopes)
        if error == 'invalid_client':
            return _err('invalid_client', 'unknown client or bad secret', 401)
        if error:
            return _err(error, 'requested scope not allowed for this client')
        return JSONResponse(result, headers={'Cache-Control': 'no-store', 'Pragma': 'no-cache'})

    async def login(request):
        store = provider.store
        if request.method == 'GET':
            req_id = request.query_params.get('req', '')
            pend = store.pop_pending(req_id, consume=False)
            if not pend:
                return HTMLResponse('<p>Authorization request expired. Start again from your client.</p>', 400)
            p = json.loads(pend['params_json'])
            return HTMLResponse(LOGIN_PAGE.format(client=escape(pend['client_id']),
                                                  scopes=escape(' '.join(p.get('scopes') or ALL_SCOPES)),
                                                  req=escape(req_id), error=''))
        form = await request.form()
        req_id = str(form.get('req', ''))
        ok = False
        if form.get('password') and store.has_admin_password():
            ok = store.check_admin_password(str(form['password']))
        if not ok and form.get('approval'):
            ok = store.consume_approval(str(form['approval']))
        if not ok:
            pend = store.pop_pending(req_id, consume=False)
            if not pend:
                return HTMLResponse('<p>Authorization request expired.</p>', 400)
            p = json.loads(pend['params_json'])
            return HTMLResponse(LOGIN_PAGE.format(client=escape(pend['client_id']),
                                                  scopes=escape(' '.join(p.get('scopes') or ALL_SCOPES)),
                                                  req=escape(req_id), error='<b>Not approved.</b>'), 401)
        try:
            url = provider.complete_authorization(req_id)
        except ValueError as e:
            return HTMLResponse(f'<p>{escape(str(e))}</p>', 400)
        return RedirectResponse(url, status_code=302)

    return [Route('/token', token_endpoint, methods=['POST']),
            Route('/login', login, methods=['GET', 'POST'])]
