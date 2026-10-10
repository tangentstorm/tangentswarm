"""Static API key check for swarm-mcp's Streamable HTTP mode (auth mode "apikey").

Every HTTP request must carry the key, either as ``Authorization: Bearer <key>`` or as
``X-API-Key: <key>``. A pure ASGI middleware around the whole app does the check, so a
request without a valid key gets ``401`` before any MCP handling (session manager,
DNS-rebinding checks, routing) sees it. The comparison uses ``hmac.compare_digest`` over
SHA-256 digests, so it takes constant time and does not leak the key length.

The server takes the key from the first of these that is set, and never logs it:

1. ``--api-key-file PATH``
2. ``$TANGENTSWARM_API_KEY``
3. ``$TANGENTSWARM_API_KEY_FILE``
4. ``~/.config/tangentswarm/api_key``

A key file must not be readable by group or others (chmod 600). The server refuses to
start in apikey mode when no key is set or the key is shorter than 32 characters.
``swarm-mcp --gen-api-key [PATH]`` writes a new key with mode 0600 and prints only the
path and a fingerprint.

A valid key grants every scope (tangentswarm:read and tangentswarm:shell). There is no
tool that runs an arbitrary command. start_agent only launches the registered coding
agents, and send_keys and tell_agent only type into panes that run one. Those agents can still run
commands when told to, so treat the key like an SSH private key.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import stat

from . import paths

ENV_KEY = 'TANGENTSWARM_API_KEY'
ENV_KEY_FILE = 'TANGENTSWARM_API_KEY_FILE'
KEY_PREFIX = 'tsw_'
MIN_KEY_LEN = 32


class ApiKeyError(RuntimeError):
    pass


def default_key_file():
    return paths.config_dir() / 'api_key'


def fingerprint(key: str) -> str:
    """Short SHA-256 fingerprint, safe to log or show."""
    return 'sha256:' + hashlib.sha256(key.encode()).hexdigest()[:16]


def key_configured(env=None) -> bool:
    env = os.environ if env is None else env
    return bool(env.get(ENV_KEY) or env.get(ENV_KEY_FILE))


def _read_key_file(path) -> str:
    path = os.path.expanduser(str(path))
    try:
        st = os.stat(path)
    except FileNotFoundError:
        raise ApiKeyError(f'API key file {path} does not exist '
                          f'(create one with: swarm-mcp --gen-api-key {path})') from None
    if not stat.S_ISREG(st.st_mode):
        raise ApiKeyError(f'API key file {path} is not a regular file')
    if st.st_mode & 0o077:
        raise ApiKeyError(f'API key file {path} is readable by group/others; chmod 600 it')
    with open(path) as f:
        return f.read().strip()


def load_api_key(key_file=None, env=None):
    """Return (key, source). Raises ApiKeyError when no usable key is configured."""
    env = os.environ if env is None else env
    if key_file:
        key, source = _read_key_file(key_file), str(key_file)
    elif env.get(ENV_KEY):
        key, source = env[ENV_KEY].strip(), '$' + ENV_KEY
    else:
        path = env.get(ENV_KEY_FILE) or default_key_file()
        key, source = _read_key_file(path), str(path)
    if not key:
        raise ApiKeyError(f'API key from {source} is empty')
    if len(key) < MIN_KEY_LEN:
        raise ApiKeyError(f'API key from {source} is shorter than {MIN_KEY_LEN} characters')
    return key, source


def generate_key_file(path=None) -> str:
    """Write a new random key to path with mode 0600 and return the path. Refuses to overwrite a file."""
    path = os.path.expanduser(str(path or default_key_file()))
    if os.path.exists(path):
        raise ApiKeyError(f'{path} already exists; remove it first to rotate the key')
    paths.ensure_private_dir(os.path.dirname(os.path.abspath(path)))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as f:
        f.write(KEY_PREFIX + secrets.token_urlsafe(32) + '\n')
    os.chmod(path, 0o600)
    return path


class ApiKeyMiddleware:
    """Pure ASGI middleware that requires the key on every http and websocket request.
    Lifespan events pass through.

    It is pure ASGI rather than BaseHTTPMiddleware, so SSE and streaming responses pass
    through unchanged.
    """

    def __init__(self, app, key: str):
        if not key or len(key) < MIN_KEY_LEN:
            raise ApiKeyError('ApiKeyMiddleware needs a key of at least %d characters' % MIN_KEY_LEN)
        self.app = app
        self._digest = hashlib.sha256(key.encode()).digest()

    def _matches(self, presented: str | None) -> bool:
        if not presented:
            return False
        digest = hashlib.sha256(presented.encode('utf-8', 'surrogateescape')).digest()
        return hmac.compare_digest(digest, self._digest)

    def authorized(self, scope) -> bool:
        ok = False
        for name, value in scope.get('headers') or ():
            if name == b'authorization':
                v = value.decode('latin-1').strip()
                if v[:7].lower() == 'bearer ':
                    ok |= self._matches(v[7:].strip())
            elif name == b'x-api-key':
                ok |= self._matches(value.decode('latin-1').strip())
        return ok

    async def __call__(self, scope, receive, send):
        kind = scope.get('type')
        if kind == 'lifespan':
            return await self.app(scope, receive, send)
        if kind in ('http', 'websocket') and self.authorized(scope):
            return await self.app(scope, receive, send)
        if kind == 'websocket':
            await send({'type': 'websocket.close', 'code': 1008})
            return
        body = json.dumps({'error': 'invalid_token',
                           'error_description': 'a valid API key is required '
                                                '(Authorization: Bearer <key> or X-API-Key)'}).encode()
        await send({'type': 'http.response.start', 'status': 401, 'headers': [
            (b'content-type', b'application/json'),
            (b'content-length', str(len(body)).encode()),
            (b'cache-control', b'no-store'),
            (b'www-authenticate', b'Bearer realm="tangentswarm", error="invalid_token"'),
        ]})
        await send({'type': 'http.response.body', 'body': body})
