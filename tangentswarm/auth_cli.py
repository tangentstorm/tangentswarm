"""`swarm auth ...`: administer the built-in OAuth authorization server.

  swarm auth set-password                 set the admin password (scrypt hash, 0600)
  swarm auth approve [--ttl 600]          print a one-time approval code for the login page
  swarm auth client add --name N [--scopes ...]   confidential client for client_credentials
  swarm auth client list | remove <client_id>
  swarm auth token issue --client X [--scopes ...] [--ttl 30d]   pre-issued bearer token
  swarm auth token list [--all] | revoke <token_id>
  swarm auth show-config

Secrets (client secrets, tokens, approval codes) are printed ONCE and only
their SHA-256 hashes are stored in ~/.local/state/tangentswarm/auth.db.
"""
import argparse
import getpass
import json
import re
import sys
import time

from . import auth as A


def parse_ttl(text):
    """'3600', '90m', '12h', '30d', '0'/'never' -> seconds (0 = no expiry)."""
    if text in (None, '', '0', 'never', 'none'):
        return 0
    m = re.fullmatch(r'(\d+)([smhdw]?)', text.strip())
    if not m:
        raise argparse.ArgumentTypeError(f'bad ttl: {text}')
    mult = {'': 1, 's': 1, 'm': 60, 'h': 3600, 'd': 86400, 'w': 604800}[m.group(2)]
    return int(m.group(1)) * mult


def _scopes(values):
    scopes = []
    for v in values or []:
        scopes += v.replace(',', ' ').split()
    scopes = scopes or list(A.ALL_SCOPES)
    bad = [s for s in scopes if s not in A.ALL_SCOPES]
    if bad:
        raise SystemExit(f'unknown scope(s): {bad}; valid: {A.ALL_SCOPES}')
    if A.SCOPE_READ not in scopes:
        print(f'note: every request needs {A.SCOPE_READ}; adding it', file=sys.stderr)
        scopes.insert(0, A.SCOPE_READ)
    return scopes


def _when(ts):
    return 'never' if not ts else time.strftime('%Y-%m-%d %H:%M %Z', time.localtime(ts))


def main(argv):
    ap = argparse.ArgumentParser(prog='swarm auth', description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('set-password')
    p = sub.add_parser('approve')
    p.add_argument('--ttl', type=parse_ttl, default=600)
    pc = sub.add_parser('client').add_subparsers(dest='sub', required=True)
    p = pc.add_parser('add')
    p.add_argument('--name', required=True)
    p.add_argument('--scopes', nargs='*')
    pc.add_parser('list')
    p = pc.add_parser('remove')
    p.add_argument('client_id')
    pt = sub.add_parser('token').add_subparsers(dest='sub', required=True)
    p = pt.add_parser('issue')
    p.add_argument('--client', required=True, help='label / client id the token is for (e.g. my-client)')
    p.add_argument('--scopes', nargs='*')
    p.add_argument('--ttl', type=parse_ttl, default=parse_ttl('30d'), help='e.g. 12h, 30d, never (default 30d)')
    p.add_argument('--label')
    p = pt.add_parser('list')
    p.add_argument('--all', action='store_true')
    p = pt.add_parser('revoke')
    p.add_argument('token_id')
    sub.add_parser('show-config')
    a = ap.parse_args(argv)

    if a.cmd == 'show-config':
        cfg = A.load_auth_config()
        d = dict(cfg.__dict__)
        if d.get('introspection_client_secret'):
            d['introspection_client_secret'] = '***'
        print(json.dumps({'config_file': str(A.config_file()), **d}, indent=2))
        return 0

    store = A.AuthStore()
    if a.cmd == 'set-password':
        pw = getpass.getpass('New admin password: ')
        if len(pw) < 10:
            print('use at least 10 characters', file=sys.stderr)
            return 1
        if getpass.getpass('Again: ') != pw:
            print('passwords differ', file=sys.stderr)
            return 1
        store.set_admin_password(pw)
        print(f'admin password hash written to {store.admin_file} (mode 600)')
    elif a.cmd == 'approve':
        code = store.create_approval(a.ttl)
        print(f'one-time approval code (valid {a.ttl}s): {code}')
    elif a.cmd == 'client':
        if a.sub == 'add':
            cid, secret = A.create_confidential_client(store, a.name, _scopes(a.scopes))
            print(f'client_id:     {cid}')
            print(f'client_secret: {secret}')
            print('(the secret is shown only now; only its hash is stored)')
        elif a.sub == 'list':
            for c in store.list_clients():
                info = json.loads(c['info_json'])
                print(f"{c['client_id']}  {c['kind']:<12} {info.get('client_name', '')!s:<20} "
                      f"scope={info.get('scope', '')}  created={_when(c['created'])}")
        elif a.sub == 'remove':
            store.remove_client(a.client_id)
            print(f'removed {a.client_id} and revoked its tokens')
    elif a.cmd == 'token':
        if a.sub == 'issue':
            token, tid, exp = store.issue_token(a.client, _scopes(a.scopes), a.ttl, 'access',
                                                None, a.client, a.label or 'pre-issued')
            print(f'token_id: {tid}')
            print(f'token:    {token}')
            print(f'expires:  {_when(exp)}')
            print('(the token is shown only now; only its hash is stored; revoke with '
                  f'`swarm auth token revoke {tid}`)')
        elif a.sub == 'list':
            for t in store.list_tokens(include_revoked=a.all):
                print(f"{t['token_id']}  {t['kind']:<7} client={t['client_id']:<16} scopes={t['scopes']}"
                      f"  expires={_when(t['expires_at'])}  {'REVOKED' if t['revoked'] else ''} {t['label'] or ''}")
        elif a.sub == 'revoke':
            store.revoke_token(token_id=a.token_id)
            print(f'revoked {a.token_id}')
    return 0


if __name__ == "__main__":
    sys.exit(main())
