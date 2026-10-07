"""`swarm cloud ...` subcommands (scialect's cli.mts, server.mts, client.mts,
the vite configs, plus the manual login flow)."""
import argparse
import asyncio
import json
import sys

from . import protocol as P

USAGE_EPILOG = """
commands:
  login                 open a headed browser at claude.ai/code; sign in by hand
  list                  list every session in the sidebar   (one-shot browser)
  status <name>         status of one session                 (one-shot browser)
  open <name>           open a session and print its latest reply (one-shot)
  wait                  park a headed browser until Ctrl-C (scialect's login aid)
  serve                 browser + websocket hub (default ws://127.0.0.1:5002/ws)
  orchestrator          thin hub on :5002 that polls the swarm and relays to :5003
  client                interactive REPL against the hub
  sessions              list sessions through the running hub (no new browser)
"""


def _need_extra():
    try:
        import playwright  # noqa: F401
        import websockets  # noqa: F401
    except ImportError as e:
        print(f"tangentswarm cloud needs the optional extra: pip install 'tangentswarm[cloud]' "
              f"&& playwright install chromium  ({e})", file=sys.stderr)
        sys.exit(2)


async def _one_shot(cmd, name, profile_dir, headed):
    from .browser import CLAUDE_CODE_URL, goto_claude_code, launch_browser
    from . import sessions as S
    handle = await launch_browser(profile_dir, headed=headed)
    try:
        if cmd == 'wait':
            await handle.page.goto(CLAUDE_CODE_URL, wait_until='domcontentloaded')
            print('Browser open. Log in to claude.ai/code, then Ctrl-C to exit.')
            await asyncio.Future()
        await goto_claude_code(handle.page)
        if cmd == 'list':
            rows = await S.list_sessions(handle.page)
            if not rows:
                print('(no sessions found in sidebar)')
            for s in rows:
                print(f"{'📌' if s['pinned'] else '  '} [{s['status']:<8}] {s['name']}")
        elif cmd == 'status':
            print(await S.get_session_status(handle.page, name))
        elif cmd == 'open':
            await S.open_session(handle.page, name)
            print(await S.get_latest_response(handle.page) or '(no assistant message yet)')
    finally:
        await handle.close()


def main(argv):
    ap = argparse.ArgumentParser(prog='swarm cloud', epilog=USAGE_EPILOG,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('command', choices=['login', 'list', 'status', 'open', 'wait', 'serve',
                                        'orchestrator', 'client', 'sessions'])
    ap.add_argument('name', nargs='*', help='session name (status/open)')
    ap.add_argument('--profile-dir', help='Chromium profile dir (default ~/.local/share/tangentswarm/playwright-profile)')
    ap.add_argument('--headless', action='store_true', help='run the browser headless (after login)')
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--port', type=int, default=None)
    ap.add_argument('--cloud-url', default=f"ws://127.0.0.1:{P.CLOUD_PORT}{P.WS_PATH}")
    ap.add_argument('--url', default=None, help='hub URL for client/sessions (default $TANGENTSWARM_CLOUD_URL or ws://127.0.0.1:5002/ws)')
    ap.add_argument('--control-dir', default=None, help='dir with workers.jsonl (orchestrator)')
    ap.add_argument('--timeout', type=int, default=900, help='login wait, seconds')
    a = ap.parse_args(argv)
    name = ' '.join(a.name).strip()

    if a.command in ('client', 'sessions', 'orchestrator'):
        try:
            import websockets  # noqa: F401
        except ImportError:
            _need_extra()
    else:
        _need_extra()

    try:
        if a.command == 'login':
            from .browser import login
            ok = asyncio.run(login(a.profile_dir, timeout=a.timeout))
            return 0 if ok else 1
        if a.command in ('list', 'status', 'open', 'wait'):
            if a.command in ('status', 'open') and not name:
                print(f'usage: swarm cloud {a.command} "<session name>"', file=sys.stderr)
                return 2
            asyncio.run(_one_shot(a.command, name, a.profile_dir, headed=not a.headless))
            return 0
        if a.command == 'serve':
            from .hub import run_server
            run_server(a.host, a.port or P.DEFAULT_PORT, headed=not a.headless, profile_dir=a.profile_dir)
            return 0
        if a.command == 'orchestrator':
            from .orchestrator import serve_orchestrator
            asyncio.run(serve_orchestrator(a.host, a.port or P.DEFAULT_PORT, a.cloud_url, a.control_dir))
            return 0
        if a.command == 'client':
            from .client import repl
            asyncio.run(repl(a.url))
            return 0
        if a.command == 'sessions':
            from .client import cloud_list_sessions
            print(json.dumps(asyncio.run(cloud_list_sessions(a.url)), indent=2))
            return 0
    except KeyboardInterrupt:
        return 130
    return 0
