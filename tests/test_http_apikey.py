"""Streamable HTTP with a static API key (auth mode 'apikey')."""
import json
import os

import pytest

from tangentswarm import apikey as K
from tangentswarm import mcp_server
from test_http_auth import Mcp, Server, http

KEY = 'tsw_' + 'k' * 40


def key_file(tmp_path, key=KEY, mode=0o600):
    p = tmp_path / 'api_key'
    p.write_text(key + '\n')
    os.chmod(p, mode)
    return p


class KeyMcp(Mcp):
    def __init__(self, srv, key, header='bearer'):
        super().__init__(srv, None)
        self.key, self.header = key, header

    def post(self, body):
        h = {'Accept': 'application/json, text/event-stream', 'MCP-Protocol-Version': '2025-06-18'}
        if self.key is not None:
            if self.header == 'bearer':
                h['Authorization'] = f'Bearer {self.key}'
            else:
                h['X-API-Key'] = self.key
        if self.session:
            h['Mcp-Session-Id'] = self.session
        status, headers, text = http('POST', self.url, body, h)
        sid = {k.lower(): v for k, v in headers.items()}.get('mcp-session-id')
        if sid:
            self.session = sid
        return status, headers, text


def test_missing_and_wrong_key_get_401_everywhere(tmp_path):
    with Server({'api_key_file': str(key_file(tmp_path))}) as srv:
        assert srv.cfg.mode == 'apikey'
        assert KeyMcp(srv, None).initialize()[0] == 401
        assert KeyMcp(srv, KEY + 'x').initialize()[0] == 401
        assert KeyMcp(srv, KEY[:-1]).initialize()[0] == 401
        assert KeyMcp(srv, 'wrong', header='x-api-key').initialize()[0] == 401
        st, h, body = http('POST', srv.base + '/mcp', {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'},
                           {'Authorization': f'Basic {KEY}', 'Accept': 'application/json, text/event-stream'})
        assert st == 401 and json.loads(body)['error'] == 'invalid_token'
        assert {k.lower(): v for k, v in h.items()}['www-authenticate'].startswith('Bearer')
        # no OAuth routes, and even unknown paths are gated (401, not 404)
        for p in ('/.well-known/oauth-authorization-server', '/register', '/token', '/login', '/nope'):
            assert http('GET', srv.base + p)[0] == 401


@pytest.mark.parametrize('header', ['bearer', 'x-api-key'])
def test_valid_key_initializes_lists_and_runs_tools(tmp_path, header):
    with Server({'api_key_file': str(key_file(tmp_path))}) as srv:
        m = KeyMcp(srv, KEY, header)
        assert m.initialize()[0] == 200
        tools = [t['name'] for t in m.call('tools/list')['result']['tools']]
        assert 'shell_exec' in tools and not [t for t in tools if 'kill' in t]
        res = m.call('tools/call', {'name': 'shell_exec', 'arguments': {'command': 'echo hi', 'cwd': '/tmp'}})
        assert not res['result'].get('isError') and '"stdout": "hi\\n"' in res['result']['content'][0]['text']
        log = json.loads(open(K.paths.state_dir() / 'shell_exec.log').read().splitlines()[-1])
        assert log['principal']['client_id'] == 'api-key'
        assert KEY not in open(K.paths.state_dir() / 'shell_exec.log').read()


def test_env_key_selects_apikey_mode(monkeypatch):
    monkeypatch.setenv(K.ENV_KEY, KEY)
    with Server() as srv:
        assert srv.cfg.mode == 'apikey' and srv.provider is None
        assert KeyMcp(srv, None).initialize()[0] == 401
        assert KeyMcp(srv, KEY).initialize()[0] == 200


def test_refuses_to_start_without_a_usable_key(tmp_path, monkeypatch):
    with pytest.raises(K.ApiKeyError, match='does not exist'):
        mcp_server.build_http_app('127.0.0.1', 1, '/mcp', {'mode': 'apikey'})
    with pytest.raises(K.ApiKeyError, match='chmod 600'):
        mcp_server.build_http_app('127.0.0.1', 1, '/mcp', {'api_key_file': str(key_file(tmp_path, mode=0o644))})
    with pytest.raises(K.ApiKeyError, match='shorter'):
        mcp_server.build_http_app('127.0.0.1', 1, '/mcp', {'api_key_file': str(key_file(tmp_path, key='short'))})
    with pytest.raises(K.ApiKeyError, match='only applies'):
        mcp_server.build_http_app('127.0.0.1', 1, '/mcp', {'mode': 'builtin', 'api_key_file': str(key_file(tmp_path))})
    with pytest.raises(SystemExit) as e:
        mcp_server.main(['--http', '--auth-mode', 'apikey', '--port', '1'])
    assert e.value.code == 2


def test_gen_api_key_writes_private_file_without_printing(tmp_path, capsys):
    p = tmp_path / 'sub' / 'api_key'
    mcp_server.main(['--gen-api-key', str(p)])
    key, _ = K.load_api_key(p)
    assert key.startswith('tsw_') and len(key) >= 40 and oct(p.stat().st_mode & 0o777) == '0o600'
    out = capsys.readouterr()
    assert key not in out.out + out.err and K.fingerprint(key) in out.err
    with pytest.raises(SystemExit):
        mcp_server.main(['--gen-api-key', str(p)])      # never overwrites
    assert K.load_api_key(p)[0] == key
