"""Streamable HTTP app builder shared by swarm-mcp (tmux control) and swarm-agent
(the agent-side message tools).

Both servers use the same three auth modes (see auth.py and apikey.py):

* apikey: one static key, checked by ApiKeyMiddleware before anything else runs.
* builtin: the server is its own OAuth authorization server (shared sqlite store).
* external: the server is only a resource server. Tokens come from an outside issuer,
  or from the builtin authorization server of a swarm-mcp on the same host (its
  tsw_ tokens are checked against the shared store).

An HttpProfile holds what differs between the two servers: the scopes, where the API key
comes from, and whether a token's RFC 8707 resource must name this server exactly.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urlsplit

from . import apikey as K
from . import auth as A


@dataclass
class HttpProfile:
    name: str                              # for log lines and error hints, e.g. 'swarm-mcp'
    build_server: Callable                 # build_server(**kwargs) -> MCPServer
    required_scopes: list[str] | None      # None: take them from the auth config
    registration_scopes: list[str] = field(default_factory=lambda: list(A.ISSUABLE_SCOPES))
    key_env: str = K.ENV_KEY
    key_file_env: str = K.ENV_KEY_FILE
    default_key_file: str | None = None    # None: ~/.config/tangentswarm/api_key
    default_file_selects_apikey: bool = False   # an existing default key file implies apikey mode
    external_checks_resource: bool = False

    def load_key(self, key_file=None):
        return K.load_api_key(key_file, env_key=self.key_env, env_key_file=self.key_file_env,
                              default_file=self.default_key_file, prog=self.name)

    def key_configured(self):
        if K.key_configured(env_key=self.key_env, env_key_file=self.key_file_env):
            return True
        return bool(self.default_file_selects_apikey and self.default_key_file
                    and os.path.exists(os.path.expanduser(self.default_key_file)))


def configured(key):
    """A setting from the shared ~/.config/tangentswarm/mcp-auth.yaml, or None."""
    import yaml
    cf = A.config_file()
    if not cf.exists():
        return None
    return (yaml.safe_load(cf.read_text()) or {}).get(key)


def resolve_mode(profile: HttpProfile, overrides):
    """The auth mode build_http_app will use: --auth-mode, then $TANGENTSWARM_AUTH_MODE,
    then apikey if a key is configured, then mcp-auth.yaml, then builtin."""
    overrides = overrides or {}
    if overrides.get('mode'):
        return overrides['mode']
    if os.environ.get('TANGENTSWARM_AUTH_MODE'):
        return os.environ['TANGENTSWARM_AUTH_MODE']
    if overrides.get('api_key_file') or profile.key_configured():
        return 'apikey'     # configuring a key selects apikey mode
    return configured('mode') or 'builtin'


def build_http_app(profile: HttpProfile, host='127.0.0.1', port=8765, path='/mcp', overrides=None):
    """Build the ASGI app for Streamable HTTP. Returns (app, config, provider)."""
    from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
    from mcp.server.transport_security import TransportSecuritySettings

    overrides = dict(overrides or {})
    overrides['mode'] = resolve_mode(profile, overrides)
    api_key_file = overrides.pop('api_key_file', None)
    if profile.required_scopes is not None:
        overrides['required_scopes'] = list(profile.required_scopes)
    cfg = A.load_auth_config(host, port, path, overrides)
    if api_key_file and cfg.mode != 'apikey':
        raise K.ApiKeyError(f'--api-key-file only applies to --auth-mode apikey (mode is {cfg.mode})')
    provider = None
    api_key = None
    if cfg.mode == 'apikey':
        api_key, cfg.api_key_source = profile.load_key(api_key_file)   # refuses to start without one
        cfg.api_key_fingerprint = K.fingerprint(api_key)
        server = profile.build_server()
    elif cfg.mode == 'builtin':
        provider = A.BuiltinProvider(cfg)
        settings = AuthSettings(
            issuer_url=cfg.issuer_url, resource_server_url=cfg.resource_url,
            required_scopes=cfg.required_scopes, validate_token_resource=True,
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=profile.registration_scopes,
                default_scopes=profile.registration_scopes),
            revocation_options=RevocationOptions(enabled=True))
        server = profile.build_server(auth=settings, auth_server_provider=provider)
    else:
        settings = AuthSettings(issuer_url=cfg.issuer_url, resource_server_url=cfg.resource_url,
                                required_scopes=cfg.required_scopes,
                                validate_token_resource=profile.external_checks_resource)
        server = profile.build_server(auth=settings, token_verifier=A.ExternalTokenVerifier(cfg))

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
        # outermost layer, so a request without the key reaches nothing
        app = K.ApiKeyMiddleware(app, api_key)
    return app, cfg, provider


def describe(cfg):
    """The auth part of the startup log line. Never includes a secret."""
    if cfg.mode == 'apikey':
        return f"auth=apikey, key={cfg.api_key_source} {cfg.api_key_fingerprint}"
    return f"auth={cfg.mode}, issuer={cfg.issuer_url}"
