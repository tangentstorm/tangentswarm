<img width="2300" height="1362" alt="image" src="https://github.com/user-attachments/assets/8bdd8260-9f16-4a8c-9805-76ea03e7884c" />

# TangentSwarm

TangentSwarm is a developer productivity tool that makes working with multiple branches in Git repositories easier,
by automating the creation of tmux sessions with customizable workspaces.

The intent is to assign each branch to a separate instance of an AI agent like Claude Code or OpenAI codex.

## Features

- Automatically clone repositories and check out branches
- Create consistent development environments with tmux
- Manage multiple branches across different repositories
- Configure custom programs to run in each pane with flexible layouts
- Automatically assign ports to different branches
- Run initialization commands for new repositories
- Support for dynamic port substitution in commands
- Persist configuration between sessions
- Easy identification of sessions with port/branch naming

## Installation

tangentswarm is a Python package (Python 3.10+, tmux required):

```sh
pip install .                     # or: pip install 'git+https://github.com/tangentstorm/tangentswarm'
pip install '.[cloud]'            # optional: Claude Code cloud sessions (Playwright + websockets)
playwright install chromium       # ...plus a browser for the cloud extra
```

This installs two commands:

- `swarm` -- the branch/tmux launcher described below (same commands as before), plus
  the agent tools ported from scialect (see [Agents and the swarm state machine](#agents-and-the-swarm-state-machine)).
- `swarm-mcp` -- an MCP server for tmux, agents and cloud sessions (see [MCP server](#mcp-server)).

`python swarm.py ...` from a checkout still works (it is now a thin shim around
`tangentswarm.cli`).

## Usage

```bash
# Simple usage with default repository
./swarm.py <branch-name>

# Specify repository and branch
./swarm.py <repo-name> <branch-name>

# View status of all branches
./swarm.py -c status
```

## Configuration

TangentSwarm uses a YAML configuration file (`~/.swarm.yaml`) in your home directory to store repository information, branch-to-port mappings, programs to run, initialization commands, and environment variables. This allows you to run the status command from any directory.

Example configuration:

```yaml
# Global swarm configuration
.swarm:
  root: ~/projects  # Root directory for all branch directories

git@github.com:username/repo:
  branches:
    main: 5000
    feature1: 5010
    feature2: 5020
    # Branch with custom environment variables
    custom-branch:
      port: 5030
      env:
        APP: custom
        DEBUG: 1
  # Repository-level environment variables (applied to all branches)
  env:
    APP: default
    NODE_ENV: development
  programs:
    - 'codex'
    - '| npm run dev --port=${PORT}'
    - '~ python api/server.py --port=${PORT+1}'
  init:
    - 'npm install'
    - 'pip install -r requirements.txt'
```

### Configuration Fields

- `.swarm`: Global configuration options
  - `root`: Root directory where all branch directories will be created (e.g. `~/projects`)
- `branches`: Maps branch names to port numbers or configuration dictionaries
  - Simple port format: `branch_name: port_number`
  - Advanced format with environment: `branch_name: { port: port_number, env: { KEY: VALUE } }`
- `env`: Repository-level environment variables applied to all programs and initialization commands
- `programs`: List of commands to run in each pane
  - First command: Always runs in the initial pane/window
  - Subsequent commands use layout prefixes to determine window/pane arrangement
- `init`: List of commands to run when setting up a new repository

### Environment Variables

TangentSwarm supports environment variables at both repository and branch levels:

1. Repository-level environment variables are applied to all branches.
2. Branch-level environment variables override repository-level ones when there are conflicts.
3. Environment variables are available to all commands in the `programs` list and `init` commands.

Example usage:

```yaml
git@github.com:username/repo:
  branches:
    # Simple branch with just a port
    main: 5000
    # Branch with custom environment
    dev:
      port: 5010
      env:
        APP: developer
        DEBUG: 1
  # Repository-level environment
  env:
    APP: default
    NODE_ENV: development
  programs:
    - 'codex'
    - '| APP=${APP} npm run dev --port=${PORT}'
```

In this example:
- For the `main` branch: `APP=default` and `NODE_ENV=development`
- For the `dev` branch: `APP=developer` (overrides repo setting), `DEBUG=1` and `NODE_ENV=development`

### Session Naming

TangentSwarm uses a naming convention for tmux sessions that includes both the port number and branch name:
```
PORT/branch_name
```

For example:
- `5000/main`
- `5010/feature-branch`

This makes it easy to identify which port is associated with each branch in the tmux session list.

### Command Sigils

Each command in the `programs` list can have a sigil (special character prefix):

- `*` - Create a new window (default if no sigil is specified)
- `|` - Create a horizontal split (side by side)
- `~` - Create a vertical split (one above the other)
- `@` - Run a tmux command against this session (e.g., `@ next-window`)
- `!` - Run command directly outside of tmux (useful for one-off commands like setting status)

Examples:
```yaml
programs:
  - 'codex'                                  # First pane of initial window
  - '| vite --port=${PORT}'                  # Horizontal split (side by side)
  - '~ python api/server.py --port=${PORT+1}' # Vertical split in the second pane
  - '* npm test'                             # New window
  - '@ next-window'                          # Run tmux command against session
  - '! echo "Working on feature X" > .swarm-status'  # Set status (runs directly)
  - '! make build'                           # Run command outside of tmux
```

To switch to the next window after setup, you can add `@ next-window` to your program list.

### Port Variable Substitution

You can use the following variables in your commands:
- `${PORT}`: Will be replaced with the branch's assigned port number
- `${PORT+n}`: Will be replaced with the branch's port plus n (where n is a digit 0-9)

Example:
```yaml
programs:
  - 'codex'
  - '| vite --port=${PORT}'
  - '~ flask run --port=${PORT+1}'
```

If the branch's port is 5000, this will run:
- `codex` in the first pane
- `vite --port=5000` in a horizontal split
- `flask run --port=5001` in a vertical split of the second pane

## How It Works

1. TangentSwarm checks if the requested branch exists in the configuration file
2. If not, it assigns a new port number and adds it to the configuration
3. It creates a directory for the repository/branch if it doesn't exist
4. It clones the repository and checks out the branch
5. For new repositories, it runs the initialization commands (and asks to continue if any fail)
6. It creates a tmux session with the layout specified by the command prefixes
7. It launches the configured programs in each pane, substituting port variables
8. Finally, it attaches to the tmux session

## Default Setup

By default, if no layout prefixes are specified, TangentSwarm will create a new window for each command:

1. Initial pane: First command (default: `codex`)
2. Window 1: Second command
3. Window 2: Third command

## Branch Status

TangentSwarm includes a status command that helps you keep track of your branches and their current states:

```bash
./swarm.py -c status
```

This command functions as an interactive session manager:

1. Displays inactive repositories and branches (those without local directories)
2. Shows active branches without tmux sessions (those with directories but no tmux session)
3. Lists ALL tmux sessions (both swarm-managed and external) in a numbered selector
4. Lets you switch to any tmux session by pressing the corresponding number key

The display shows each tmux session with its full name (which includes the port number for swarm-managed sessions) and status information from the `.swarm-status` file (if present). This creates a clean tmux session selector that makes it easy to keep track of all your tmux sessions and branches in one view.

### Status Files

You can create a `.swarm-status` file in the root of your branch directory with a single line of text:

```
Working on feature X
```

This status message will be displayed when you run `swarm.py -c status`, allowing you to keep notes about what you're working on in each branch.

## Tips

- Customize the programs and their layout for each repository in the YAML config file
- Add initialization commands to automate repository setup
- Use branch-specific configurations when needed
- Use port variables to ensure services use the correct ports
- If already in a tmux session, TangentSwarm will switch to the new session rather than nesting
- Use `tmux ls` to view all running sessions with their port numbers
- Use `set -g status-left-length 50` to increase the length of the tmux session name display
- Add `bind s choose-tree -s -O name` to your `~/.tmux.conf` to sort sessions alphabetically when you press `<prefix> s`. Since TangentSwarm uses `port/name` format, this effectively sorts sessions by port number
- Replace the default tmux session chooser with swarm's status command by adding this to your `~/.tmux.conf`:
  ```
  bind-key s run-shell "tmux split-window -p 70 'python /path/to/swarm.py -c status'"
  ```
  (Replace `/path/to/swarm.py` with the absolute path to your swarm.py file. Since the config is in `~/.swarm.yaml`, you can run this from any directory.)
- If you want certain sessions to appear at the top of the sorted list, you can rename them with `<prefix> : rename-session *important-session`. The asterisk (`*`) character sorts before numbers, causing these sessions to appear first in the list. Note that most other characters that would sort before digits are invalid in tmux session names


## Requirements

- Python 3.10+ (required for match/case statements)
- tmux
- Git
- PyYAML, `mcp` (>=2.3), PyJWT, uvicorn (installed by pip)
- optional `[cloud]` extra: Playwright (+ `playwright install chromium`), websockets

## Package layout

```
tangentswarm/
  cli.py           swarm CLI (the original swarm.py) + subcommand dispatch
  tmux.py          tmux wrappers (argv lists only; structured list_sessions/list_panes)
  git.py           git helpers
  mcp_server.py    swarm-mcp (stdio, or Streamable HTTP with OAuth)
  shell.py         shell_exec with timeout, truncation and JSON-lines logging
  auth.py          OAuth resource server + optional built-in authorization server
  auth_cli.py      swarm auth ...
  agents.py        agent detection, prompt-empty detection, space probe, tell/wait
  workers.py       workers.jsonl / known-agents.jsonl
  local_status.py  swarm status table          (scialect local-status)
  tell_worker.py   state-machine handoffs      (scialect tell-worker)
  local_step.py    propose/run next handoff    (scialect local-step)
  for_all.py       run a command in every worker dir (scialect for-all)
  rule_deps.py     `uses:` closure of prompt guides (scialect rule-deps)
  swarm_state.py   swarm-status deltas         (scialect swarm.mts)
  cloud/           Claude Code cloud sessions  (scialect browser/sessions/server/client/...)
```

## Agents and the swarm state machine

These are Python ports of [scialect](https://github.com/tangentstorm/scialect)'s agent
tooling. Like scialect they run in a *control directory* (the current directory) holding
`workers.jsonl` (`{"id","dir","session","window"}` per line), optionally
`known-agents.jsonl` (built-in defaults: claude, codex, gemini/agy, opencode) and a
git-tracked `rules/` directory of prompt guides.

```sh
swarm -c local-status                       # id | agent | state | health | status
swarm -c tell-worker jc3 accept             # assigned|accept|plan-approved|adjust|unblocked|reject|rebase [branch]
swarm -c tell-worker mgr review jc3         # review|approve-task|unblock <worker>
swarm -c step                               # propose the next transition, confirm, run it
swarm -c for-all 'git status -s'
swarm -c agent-status agents:1              # which agent, is its prompt blank?
swarm -c tell-agent agents:1 'please run the tests'
```

Sending to an agent always follows scialect's tell-worker sequence: reach an empty
prompt first (a non-destructive *space probe*: type a space, check, backspace -- this sees
past placeholder text but refuses when a human is typing), optionally `/new` + Enter +
10s, then type the text literally, wait 0.5s, and press Enter in a separate `send-keys`
(TUIs drop an Enter that arrives with the text). Handoffs are atomic: worker state
(`.sci/status-line`, guides) is only written after the message was delivered.

`swarm cloud` and `swarm auth` are reserved words; the other new commands live behind
`-c` so `swarm [<repo>] <branch>` keeps its old meaning.

## Claude Code cloud sessions (optional)

Requires `pip install 'tangentswarm[cloud]'` and `playwright install chromium`.

```sh
swarm cloud login          # headed browser at claude.ai/code -- sign in by hand once
swarm cloud serve          # browser + websocket hub on ws://127.0.0.1:5002/ws
swarm cloud client         # REPL: /list /use <name> /status [name] /latest /help /quit
swarm cloud list | status "<name>" | open "<name>" | wait     # one-shot browser
swarm cloud serve --port 5003 & swarm cloud orchestrator      # scialect's split setup:
                           # thin :5002 hub polls the swarm, pushes swarm-status, relays to :5003
```

The login cookie lives in a persistent Chromium profile **outside the repo**,
`~/.local/share/tangentswarm/playwright-profile` (override with
`TANGENTSWARM_PROFILE_DIR` or `--profile-dir`). Optionally `TANGENTSWARM_STORAGE_STATE`
can point at a Playwright storage-state JSON (also outside the repo) whose cookies are
loaded at launch. No credentials are stored in the repository. The first login needs a
display: run `swarm cloud login` on a desktop (or over `ssh -X`) and copy the profile
directory if needed; afterwards `--headless` works. Shut the server down with Ctrl-C (not
SIGKILL) so the cookie is flushed.

The websocket protocol is unchanged from scialect (JSON frames, `id`-correlated replies,
`kind: "event"` pushes; see scialect's `docs/websocket-agent.md`). The hub binds
127.0.0.1 and has no authentication -- never expose port 5002/5003.

## MCP server

`swarm-mcp` speaks MCP over **stdio** (default) or **Streamable HTTP** (`--http`).

| tool | scope (HTTP) | what it does |
| --- | --- | --- |
| `list_sessions` | read | tmux sessions: name, id, windows, attached, created |
| `list_panes(target?, all?)` | read | panes: session, window index/name, pane index/id, active, command, path, size |
| `capture_pane(target, history_lines?, escapes?)` | read | pane text (`-S -N` scrollback, `-e` escapes) |
| `send_keys(target, text, enter=true, literal=true)` | shell | literal text, 0.5s, separate Enter; `literal=false` for key names |
| `new_session(name, cwd?, command?)` | shell | detached session |
| `new_window(session, name?, cwd?, command?)` | shell | window at the next index, no client switch |
| `shell_exec(command, cwd?, timeout=60)` | shell | `bash -lc`, max 600s; exit_code/stdout/stderr/timed_out; logged |
| `agent_status(target)` | read | detected agent, prompt blank?, last lines |
| `pane_ready(target, probe=false)` | read (probe: shell) | is the agent's prompt empty |
| `wait_for_idle(target, timeout_sec=120, poll_ms, settle_sec)` | read | screen stable + prompt blank |
| `tell_agent(target, text, new_conversation?, require_empty_prompt=true)` | shell | tell-worker send sequence |
| `swarm_status(control_dir)` | read | local-status rows |
| `tell_worker(control_dir, worker, verb, arg?)` | shell | state-machine handoff |
| `cloud_list_sessions` | read | claude.ai/code sidebar sessions (needs `swarm cloud serve`) |
| `cloud_send_message(session_id, text)` | shell | send to a cloud session |
| `cloud_get_latest_response(session_id)` | read | last transcript message |
| `cloud_wait_for_response(session_id, text?, timeout_sec=120, poll_ms=1500)` | shell | send, then poll until settled (max 600s) |

There are deliberately **no** kill-pane / kill-window / kill-session tools.

Every `shell_exec` call is appended as a JSON line to
`~/.local/state/tangentswarm/shell_exec.log` (timestamp, cwd, command, timeout, exit
code, duration, timed_out, output lengths, and the OAuth client in HTTP mode -- never the
output) and echoed to stderr. stdout carries nothing but MCP protocol traffic.

### stdio over SSH (recommended)

Give the server its own SSH key with a forced command in the target user's
`~/.ssh/authorized_keys`:

```
command="/home/memnar/.venvs/tangentswarm/bin/swarm-mcp",no-port-forwarding,no-X11-forwarding,no-agent-forwarding,no-pty ssh-ed25519 AAAA... skeletor-swarm-mcp
```

and point the MCP client at ssh:

```json
{
  "mcpServers": {
    "tangentswarm": {
      "command": "ssh",
      "args": ["-i", "~/.ssh/tangentcode_memnar_swarm_mcp", "-o", "IdentitiesOnly=yes", "-T",
               "memnar@tangentcode.com", "swarm-mcp"]
    }
  }
}
```

(The forced command runs regardless of the trailing `swarm-mcp` argument.)

### Streamable HTTP with OAuth 2.0

```sh
swarm-mcp --http --host 127.0.0.1 --port 8765        # endpoint: http://127.0.0.1:8765/mcp
```

The server is an OAuth 2.0 **resource server** built on the MCP SDK's auth support:

- `GET /.well-known/oauth-protected-resource/mcp` -- Protected Resource Metadata (RFC 9728)
  naming the authorization server.
- Requests without a valid bearer token get `401` with
  `WWW-Authenticate: Bearer ... resource_metadata="..."`; a token lacking
  `tangentswarm:read` gets `403 insufficient_scope`.
- `tangentswarm:read` is required for every request; tools that execute commands or type
  into panes additionally need `tangentswarm:shell` (see the table).

Two authorization-server modes (`TANGENTSWARM_AUTH_MODE`, default `builtin`):

**external** -- an outside OAuth/OIDC issuer. Access tokens that are JWTs are verified
against the issuer's JWKS (discovered from `/.well-known/openid-configuration` or
`/.well-known/oauth-authorization-server` unless `jwks_url` is set): signature, `iss`,
`exp`, `aud` (or a `resource` claim) must equal this server's resource URL/audience, and
scopes come from `scope`/`scp`. Opaque tokens fall back to RFC 7662 introspection when
`introspection_url` is configured. Tokens from `swarm auth token issue` are accepted too
(`accept_local_tokens`).

**builtin** -- a small authorization server in the same process (SDK
`OAuthAuthorizationServerProvider`), state in `~/.local/state/tangentswarm/auth.db` (0600):

- authorization code + PKCE with dynamic client registration (`/register`, `/authorize`,
  `/token`, `/revoke`, `/.well-known/oauth-authorization-server`) for interactive MCP
  clients. The authorize step shows `/login`, approved by the admin password
  (`swarm auth set-password`, scrypt hash in `admin.json`, 0600) or a one-time code from
  `swarm auth approve`.
- `client_credentials` for confidential clients from `swarm auth client add` (secret shown
  once, stored as a hash).
- pre-issued bearer tokens from `swarm auth token issue` (`swarm auth token list/revoke`).
- refresh-token rotation and RFC 7009 revocation.

Config keys (file `~/.config/tangentswarm/mcp-auth.yaml`, overridden by env, overridden
by CLI flags):

| key | env | default |
| --- | --- | --- |
| `mode` | `TANGENTSWARM_AUTH_MODE` | `builtin` |
| `resource_url` | `TANGENTSWARM_AUTH_RESOURCE_URL` / `--public-url` | `http://<host>:<port>/mcp` |
| `issuer_url` | `TANGENTSWARM_AUTH_ISSUER` / `--issuer` | builtin: origin of resource_url |
| `audience` | `TANGENTSWARM_AUTH_AUDIENCE` / `--audience` | resource_url |
| `jwks_url`, `jwks_file` | `TANGENTSWARM_AUTH_JWKS_URL`, `..._JWKS_FILE` | discovered |
| `required_scopes` | `TANGENTSWARM_AUTH_REQUIRED_SCOPES` | `tangentswarm:read` |
| `introspection_url` | `TANGENTSWARM_AUTH_INTROSPECTION_URL` | unset |
| `introspection_client_id` / `_secret` | `..._INTROSPECTION_CLIENT_ID` / `..._CLIENT_SECRET` (or `..._CLIENT_SECRET_FILE`) | unset |
| `accept_local_tokens` | `TANGENTSWARM_AUTH_ACCEPT_LOCAL_TOKENS` | true |
| `access_token_ttl`, `refresh_token_ttl` | -- | 3600, 30 days |

Nothing secret is committed or generated into the repo: tokens, client secrets and
approval codes are stored only as SHA-256 hashes, the admin password as scrypt, all in
0600 files under `~/.local/state/tangentswarm`.

#### Connecting a headless client (e.g. Memnar)

The HTTP endpoint is meant to sit behind loopback (or a TLS reverse proxy later). From
another machine, tunnel first: `ssh -N -L 8765:127.0.0.1:8765 user@host`.

*(a) pre-issued token* -- on the server: `swarm auth token issue --client memnar --scopes
tangentswarm:read tangentswarm:shell --ttl 30d`; then

```sh
TOKEN=tsw_...    # shown once
curl -sS http://127.0.0.1:8765/mcp -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"memnar","version":"1"}}}' -D -
# reuse the Mcp-Session-Id response header on later requests, send notifications/initialized,
# then tools/list and tools/call
```

*(b) client_credentials* -- on the server: `swarm auth client add --name memnar --scopes
tangentswarm:read tangentswarm:shell` (prints client_id and secret once); then

```sh
curl -sS -u "$CLIENT_ID:$CLIENT_SECRET" -d grant_type=client_credentials \
  http://127.0.0.1:8765/token          # -> {"access_token": "...", "expires_in": 3600, ...}
```

and use the access token as in (a); fetch a new one when it expires.

*(c) interactive clients* (auth code + PKCE): point the client at
`http://127.0.0.1:8765/mcp`; it discovers the metadata, registers itself, and opens
`/login`, where you enter the admin password or a `swarm auth approve` code. Device-code
flow is not implemented; use (a) or (b) for headless clients.

In Python, the official MCP SDK client works with a static token:

```python
import asyncio, os
from mcp import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

async def main():
    url, token = os.environ["SWARM_MCP_URL"], os.environ["SWARM_MCP_TOKEN"]
    async with create_mcp_http_client(headers={"Authorization": f"Bearer {token}"}) as http:
        async with streamable_http_client(url, http_client=http) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                print(await session.call_tool("shell_exec", {"command": "echo hi", "cwd": "/tmp"}))

asyncio.run(main())
```

#### Exposing it publicly later (not done by default)

1. Put it behind TLS: an nginx (or caddy) vhost proxying `https://swarm.example.com/` to
   `http://127.0.0.1:8765/` (keep `--host 127.0.0.1`).
2. Start it with the public resource URL so metadata, audience checks and DNS-rebinding
   protection use the public host:
   `swarm-mcp --http --host 127.0.0.1 --port 8765 --public-url https://swarm.example.com/mcp`
   (an `issuer_url` on https; for builtin mode it defaults to `https://swarm.example.com`).
3. Set an admin password (`swarm auth set-password`) or switch to an external issuer.
4. Consider issuing only `tangentswarm:read` to clients that don't need `shell_exec`.

## License

MIT
