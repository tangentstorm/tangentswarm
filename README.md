<img width="2300" height="1362" alt="image" src="https://github.com/user-attachments/assets/8bdd8260-9f16-4a8c-9805-76ea03e7884c" />

# TangentSwarm

TangentSwarm checks out each branch of a Git repository into its own directory and opens a tmux session for it, with the programs and pane layout you configure.

The idea is to give each branch its own AI agent, such as Claude Code or OpenAI Codex.

## Features

- Clones repositories and checks out branches
- Opens a tmux session per branch with the same layout each time
- Manages branches across several repositories
- Runs the programs you configure in windows and panes
- Assigns each branch its own port
- Runs initialization commands for new repositories
- Substitutes the branch's port into commands
- Saves the configuration between runs
- Names each session port/branch

## Installation

tangentswarm is a Python package (Python 3.10+, tmux required):

```sh
pip install .                     # or: pip install 'git+https://github.com/tangentstorm/tangentswarm'
pip install '.[cloud]'            # optional: Claude Code cloud sessions (Playwright + websockets)
playwright install chromium       # ...plus a browser for the cloud extra
```

This installs two commands:

- `swarm`, the branch and tmux launcher described below, plus the agent tools ported
  from scialect (see [Agents and the swarm state machine](#agents-and-the-swarm-state-machine)).
- `swarm-mcp`, an MCP server for tmux, agents and cloud sessions (see [MCP server](#mcp-server)).

## Usage

```bash
# Simple usage with default repository
swarm <branch-name>

# Specify repository and branch
swarm <repo-name> <branch-name>

# View status of all branches
swarm -c status
```

## Configuration

TangentSwarm keeps its configuration in `~/.swarm.yaml` in your home directory. The file holds the repositories, the port for each branch, the programs to run, initialization commands and environment variables. Because it lives in your home directory, the status command works from any directory.

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

### Configuration fields

- `.swarm` holds global options.
  - `root` is the directory where swarm creates the branch directories, such as `~/projects`.
- `branches` maps each branch name to a port number or a dictionary.
  - The short form is `branch_name: port_number`.
  - The long form adds environment variables: `branch_name: { port: port_number, env: { KEY: VALUE } }`.
- `env` sets repository-level environment variables for all programs and initialization commands.
- `programs` lists the commands to run.
  - The first command always runs in the initial window.
  - Each later command's sigil decides whether it gets a new window or a split.
- `init` lists the commands to run when swarm sets up a new repository.

### Environment variables

You can set environment variables for a repository and for a branch:

1. Repository-level variables apply to all branches.
2. A branch-level variable overrides a repository-level one with the same name.
3. All commands in `programs` and `init` see these variables.

Example:

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
- The `main` branch gets `APP=default` and `NODE_ENV=development`.
- The `dev` branch gets `APP=developer` (overriding the repository value), `DEBUG=1` and `NODE_ENV=development`.

### Session naming

Each tmux session name has the port number and the branch name:
```
PORT/branch_name
```

For example:
- `5000/main`
- `5010/feature-branch`

The `main` branch uses the repository name in place of `main`. The tmux session list then shows each branch's port.

### Command sigils

Each command in the `programs` list can start with a sigil, a one-character prefix:

- `*` creates a new window. This is the default when there is no sigil.
- `|` creates a horizontal split (side by side).
- `~` creates a vertical split (one above the other).
- `@` runs a tmux command against this session, such as `@ next-window`.
- `!` runs the command directly, outside tmux. Use it for one-off commands such as setting the status.

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

To switch to the next window after setup, add `@ next-window` to the list.

### Port variables

Commands can use these variables:
- `${PORT}` becomes the branch's port number.
- `${PORT+n}` becomes the branch's port plus n, where n is a digit from 0 to 9.

Example:
```yaml
programs:
  - 'codex'
  - '| vite --port=${PORT}'
  - '~ flask run --port=${PORT+1}'
```

If the branch's port is 5000, this runs:
- `codex` in the first pane
- `vite --port=5000` in a horizontal split
- `flask run --port=5001` in a vertical split of the second pane

## How it works

1. TangentSwarm checks whether the branch is in the configuration file.
2. If it is not, swarm assigns it a new port and adds it to the configuration.
3. It creates a directory for the branch if there is none.
4. It clones the repository and checks out the branch.
5. For a new repository, it runs the initialization commands, and asks whether to continue if one fails.
6. It creates a tmux session with the layout the sigils describe.
7. It starts the configured programs, with the port variables filled in.
8. It attaches to the tmux session.

## Default setup

When the commands have no sigils, TangentSwarm creates a new window for each one:

1. The initial pane runs the first command (`codex` by default).
2. Window 1 runs the second command.
3. Window 2 runs the third command.

## Branch status

The status command lists your branches and their sessions:

```bash
swarm -c status
```

It works as an interactive session picker:

1. It lists inactive repositories and branches, which have no local directory.
2. It lists active branches that have a directory but no tmux session.
3. It numbers every tmux session, including ones swarm did not create.
4. You press a session's number to switch to it.

Each session shows its full name, which includes the port for sessions swarm created, and the text of its `.swarm-status` file if there is one.

### Status files

Put a `.swarm-status` file with one line of text in the root of a branch directory:

```
Working on feature X
```

`swarm -c status` shows that line next to the branch, so you can note what you are working on in each branch.

## Tips

- Each repository in the config file can have its own programs and layout.
- Put setup steps such as `npm install` in `init`.
- Use port variables so each branch's services listen on that branch's ports.
- Inside tmux, TangentSwarm switches to the new session instead of nesting one.
- `tmux ls` lists all running sessions with their port numbers.
- `set -g status-left-length 50` gives the session name more room in the status bar.
- Add `bind s choose-tree -s -O name` to your `~/.tmux.conf` to sort sessions by name when you press `<prefix> s`. Swarm names start with the port, so this sorts them by port.
- To use swarm's status command as the tmux session chooser, add this to your `~/.tmux.conf`:
  ```
  bind-key s run-shell "tmux split-window -p 70 'swarm -c status'"
  ```
  Use the full path to `swarm` if tmux can't find it. The config is in `~/.swarm.yaml`, so this works from any directory.
- To put a session at the top of the sorted list, rename it with `<prefix> : rename-session *important-session`. `*` sorts before digits. Most other characters that sort before digits are not allowed in tmux session names.


## Requirements

- Python 3.10+
- tmux
- Git
- PyYAML, `mcp` (>=2.3), PyJWT, uvicorn (installed by pip)
- For the optional `[cloud]` extra, Playwright (plus `playwright install chromium`) and websockets

## Package layout

```
tangentswarm/
  cli.py           swarm CLI + subcommand dispatch
  tmux.py          tmux wrappers (argv lists only; structured list_sessions/list_panes)
  git.py           git helpers
  mcp_server.py    swarm-mcp (stdio, or Streamable HTTP with OAuth or an API key)
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

These commands are Python ports of [scialect](https://github.com/tangentstorm/scialect)'s
agent tools. Like scialect, they run in a control directory, which is the current
directory. It holds `workers.jsonl` (one `{"id","dir","session","window"}` per line), an
optional `known-agents.jsonl` (the built-in defaults are claude, codex, gemini/agy and
opencode), and a git-tracked `rules/` directory of prompt guides.

```sh
swarm -c local-status                       # id | agent | state | health | status
swarm -c tell-worker jc3 accept             # assigned|accept|plan-approved|adjust|unblocked|reject|rebase [branch]
swarm -c tell-worker mgr review jc3         # review|approve-task|unblock <worker>
swarm -c step                               # propose the next transition, confirm, run it
swarm -c for-all 'git status -s'
swarm -c agent-status agents:1              # which agent, is its prompt blank?
swarm -c tell-agent agents:1 'please run the tests'
```

Sending to an agent always follows scialect's tell-worker sequence.

1. Reach an empty prompt with the space probe. It types a space, checks the prompt and
   deletes the space. This sees past placeholder text but refuses when someone is typing.
2. Optionally send `/new`, press Enter and wait 10s.
3. Type the text literally, wait 0.5s and press Enter in a separate `send-keys`. TUIs
   drop an Enter that arrives with the text.

Handoffs are atomic. Swarm writes the worker state (`.sci/status-line` and the guides)
only after it delivers the message.

`swarm cloud` and `swarm auth` are reserved words. The other new commands live behind
`-c`, so `swarm [<repo>] <branch>` keeps its old meaning.

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

The login cookie lives in a persistent Chromium profile outside the repo, at
`~/.local/share/tangentswarm/playwright-profile`. `TANGENTSWARM_PROFILE_DIR` or
`--profile-dir` overrides the path. `TANGENTSWARM_STORAGE_STATE` can point at a
Playwright storage-state JSON file, also outside the repo, whose cookies the browser
loads at launch. The first login needs a display. Run `swarm cloud login` on a desktop
or over `ssh -X`, and copy the profile directory if needed. After that, `--headless`
works. Stop the server with Ctrl-C, not SIGKILL, so Chromium writes the cookie to disk.

The websocket protocol is the same as scialect's. Each frame is JSON, a reply carries
its request's `id`, and pushed events have `kind: "event"`. See scialect's
`docs/websocket-agent.md`. The hub binds 127.0.0.1 and has no authentication, so never
expose port 5002 or 5003.

## MCP server

`swarm-mcp` speaks MCP over stdio (the default) or Streamable HTTP (`--http`).

| tool | scope (HTTP) | what it does |
| --- | --- | --- |
| `list_sessions` | read | tmux sessions: name, id, windows, attached, created |
| `list_panes(target?, all?)` | read | panes: session, window index/name, pane index/id, active, command, path, size |
| `capture_pane(target, history_lines?, escapes?)` | read | pane text (`-S -N` scrollback, `-e` escapes) |
| `send_keys(target, text, enter=true, literal=true)` | shell | agent panes only: literal text, 0.5s, separate Enter; `literal=false` for key names |
| `list_agents` | read | the registered agents: installed?, binary, exact argv, adapter level; the cwd root |
| `start_agent(agent, cwd, session="agents", window_name?)` | shell | start a registered agent (enum) in a new window under the cwd root |
| `agent_status(target)` | read | registered agent in the pane's foreground, typeable?, prompt blank?, last lines |
| `pane_ready(target, probe=false)` | read (probe: shell) | is the agent's prompt empty |
| `wait_for_idle(target, timeout_sec=120, poll_ms, settle_sec)` | read | screen stable + prompt blank |
| `tell_agent(target, text, new_conversation?, require_empty_prompt=true)` | shell | agent panes only: tell-worker send sequence |
| `swarm_status(control_dir)` | read | local-status rows |
| `tell_worker(control_dir, worker, verb, arg?)` | shell | state-machine handoff (agent panes only) |
| `cloud_list_sessions` | read | claude.ai/code sidebar sessions (needs `swarm cloud serve`) |
| `cloud_send_message(session_id, text)` | shell | send to a cloud session |
| `cloud_get_latest_response(session_id)` | read | last transcript message |
| `cloud_wait_for_response(session_id, text?, timeout_sec=120, poll_ms=1500)` | shell | send, then poll until settled (max 600s) |

There are no kill-pane, kill-window or kill-session tools, and no tool that runs an
arbitrary command. `shell_exec` and the free `command` argument of `new_session` and
`new_window` are gone. You start agents with `start_agent` and drive them through their
panes with `tell_agent` and `send_keys`. stdout carries only MCP protocol traffic, and
logs go to stderr.

### Agent registry and the typing guard

`tangentswarm/registry.py` holds a fixed allowlist. Nothing else can be launched, and
the caller cannot add arguments.

| agent | binary (first that exists) | fixed flags | adapter |
| --- | --- | --- | --- |
| `claude` | `~/.npm-global/bin/claude`, `~/workspace/.npm-global/bin/claude`, ... | | full (❯ box between ─ bars; dim suggestions ignored) |
| `muse` | `~/.local/bin/muse` (launcher; execs `muse-bin-<version>`) | `--trust-workspace` | full (❯ box between ─ bars; grey placeholder ignored; `/new`) |
| `codex` | `~/.npm-global/bin/codex`, ... | | basic (› prompt, ported from scialect) |
| `gemini` | `~/.npm-global/bin/gemini`, `/usr/local/bin/gemini`, ... | | basic (> prompt, ported from scialect) |
| `grok` | `~/.grok/bin/grok` | | none: start/type only; `tell_agent` needs `require_empty_prompt=false` |

`list_agents` still lists an agent that is not installed, with `installed: false`, and
starting it fails with "not installed". The operator can point an agent at another binary
with `TANGENTSWARM_AGENT_<NAME>=/abs/path`.

- `start_agent` runs `[binary, *flags]` as the new window's own process. tmux gets the
  argv as separate arguments and runs it without a shell. Agents without flags go through
  `/usr/bin/env --`, so tmux never falls back to `sh -c`. When the agent exits, the window
  closes and leaves no shell prompt. `cwd` must resolve, after symlinks, to an existing
  directory strictly inside `$TANGENTSWARM_AGENT_ROOT` (default `~/ver`). `session` must
  match `[A-Za-z0-9_-]{1,40}`, and swarm creates it if it is missing. `window_name` is
  kebab-case, with a default of `<agent>-<dir>`.
- `send_keys`, `tell_agent`, `pane_ready(probe=true)` and `tell_worker` resolve the target
  to the exact pane with `display-message -t`, which is the pane send-keys would hit. They
  list the processes on its tty and go ahead only if a process in the terminal's
  foreground process group (`+` in the `ps` stat) is a registered agent. They refuse a
  bash prompt, a dead pane, or a shell whose agent was suspended with C-z. They then send
  keys to that pane id.
- Agent recognition looks at the process's comm, its argv[0] and, for node, bun and deno,
  the script name. It never looks at the rest of the command line, because prompts often
  mention other agents.
- Prompt detection uses an escape-coded capture. Text drawn dim or in grey (Claude's
  prompt suggestions, Muse's placeholder) counts as an empty prompt, and text in the
  normal colour counts as real input.

The guard does not stop the agents themselves from running commands. You can use
Claude's `!` bash mode or ask an agent to run something, and `send_keys` can answer
approval prompts. So a key that can talk to agents can still run commands as the
server's user. It cannot type into a shell or launch an arbitrary program directly.

### stdio over SSH (recommended)

Give the server its own SSH key with a forced command in the target user's
`~/.ssh/authorized_keys`:

```
command="/home/swarm/.venvs/tangentswarm/bin/swarm-mcp",no-port-forwarding,no-X11-forwarding,no-agent-forwarding,no-pty ssh-ed25519 AAAA... swarm-mcp-client
```

Then point the MCP client at ssh:

```json
{
  "mcpServers": {
    "tangentswarm": {
      "command": "ssh",
      "args": ["-i", "~/.ssh/swarm_mcp", "-o", "IdentitiesOnly=yes", "-T",
               "swarm@host.example", "swarm-mcp"]
    }
  }
}
```

The forced command runs whatever the trailing `swarm-mcp` argument says.

### Streamable HTTP with OAuth 2.0

```sh
swarm-mcp --http --host 127.0.0.1 --port 8765        # endpoint: http://127.0.0.1:8765/mcp
```

The server is an OAuth 2.0 resource server built on the MCP SDK's auth support.

- `GET /.well-known/oauth-protected-resource/mcp` returns the Protected Resource Metadata
  (RFC 9728), which names the authorization server.
- A request without a valid bearer token gets `401` with
  `WWW-Authenticate: Bearer ... resource_metadata="..."`. A token without
  `tangentswarm:read` gets `403 insufficient_scope`.
- Every request needs `tangentswarm:read`. Tools that type into panes or start agents
  also need `tangentswarm:shell` (see the table).

There are two authorization-server modes, set by `TANGENTSWARM_AUTH_MODE` (default `builtin`).

**External mode.** An outside OAuth or OIDC issuer signs the tokens. The server checks a
JWT access token against the issuer's JWKS, which it finds through
`/.well-known/openid-configuration` or `/.well-known/oauth-authorization-server` unless
`jwks_url` is set. It checks the signature, `iss` and `exp`. The `aud` claim, or a
`resource` claim, must equal this server's resource URL or audience. Scopes come from
`scope` or `scp`. For opaque tokens the server falls back to RFC 7662 introspection when
`introspection_url` is set. It also accepts tokens from `swarm auth token issue`
(`accept_local_tokens`).

**Builtin mode.** A small authorization server runs in the same process (the SDK's
`OAuthAuthorizationServerProvider`) and keeps its state in
`~/.local/state/tangentswarm/auth.db` (mode 0600). It supports:

- Authorization code with PKCE and dynamic client registration (`/register`, `/authorize`,
  `/token`, `/revoke`, `/.well-known/oauth-authorization-server`) for interactive MCP
  clients. The authorize step shows `/login`, where you approve with the admin password
  (`swarm auth set-password`, stored as a scrypt hash in `admin.json`, mode 0600) or a
  one-time code from `swarm auth approve`.
- `client_credentials` for confidential clients from `swarm auth client add`. The CLI
  shows the secret once and stores a hash.
- Pre-issued bearer tokens from `swarm auth token issue`, managed with
  `swarm auth token list` and `swarm auth token revoke`.
- Refresh-token rotation and RFC 7009 revocation.

The config keys live in `~/.config/tangentswarm/mcp-auth.yaml`. Environment variables
override the file, and CLI flags override both.

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
| `access_token_ttl`, `refresh_token_ttl` | none | 3600, 30 days |

No secrets go into the repo. The server stores tokens, client secrets and approval codes
only as SHA-256 hashes, and the admin password as a scrypt hash, all in 0600 files under
`~/.local/state/tangentswarm`.

#### Connecting a headless client (e.g. a script)

The HTTP endpoint listens on loopback, optionally behind a TLS reverse proxy. From
another machine, open a tunnel first with `ssh -N -L 8765:127.0.0.1:8765 user@host`.

**(a) Pre-issued token.** On the server, run `swarm auth token issue --client my-client --scopes
tangentswarm:read tangentswarm:shell --ttl 30d`. Then:

```sh
TOKEN=tsw_...    # shown once
curl -sS http://127.0.0.1:8765/mcp -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"my-client","version":"1"}}}' -D -
# reuse the Mcp-Session-Id response header on later requests, send notifications/initialized,
# then tools/list and tools/call
```

**(b) client_credentials.** On the server, run `swarm auth client add --name my-client --scopes
tangentswarm:read tangentswarm:shell`, which prints the client_id and secret once. Then:

```sh
curl -sS -u "$CLIENT_ID:$CLIENT_SECRET" -d grant_type=client_credentials \
  http://127.0.0.1:8765/token          # -> {"access_token": "...", "expires_in": 3600, ...}
```

Use the access token as in (a), and fetch a new one when it expires.

**(c) Interactive clients** (authorization code with PKCE). Point the client at
`http://127.0.0.1:8765/mcp`. It reads the metadata, registers itself and opens `/login`,
where you enter the admin password or a `swarm auth approve` code. The device-code flow
is not implemented, so use (a) or (b) for headless clients.

The MCP SDK client for Python works with a static token:

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
                print(await session.call_tool("list_sessions", {}))

asyncio.run(main())
```

### Streamable HTTP with a static API key

For a single trusted client, `--auth-mode apikey` replaces OAuth with one random key.

```sh
swarm-mcp --gen-api-key            # writes ~/.config/tangentswarm/api_key (0600), prints only a fingerprint
swarm-mcp --http --auth-mode apikey --host 127.0.0.1 --port 8766 \
          --public-url https://host.example/swarm-mcp/mcp
```

- Every request must send `Authorization: Bearer <key>` or `X-API-Key: <key>`. Any other
  request, on any path, gets `401` from an ASGI middleware around the whole app, before
  any MCP handling. The comparison takes constant time (`ApiKeyMiddleware` in
  `tangentswarm/apikey.py`).
- The server reads the key from the first of these that is set: `--api-key-file`,
  `$TANGENTSWARM_API_KEY`, `$TANGENTSWARM_API_KEY_FILE`, `~/.config/tangentswarm/api_key`.
  Key files must be mode 0600. Setting a key selects apikey mode unless `--auth-mode` or
  `TANGENTSWARM_AUTH_MODE` says otherwise. In apikey mode the server refuses to start
  without a key, or with a key shorter than 32 characters.
- A valid key grants every scope, including typing into agent panes with `send_keys` and
  `tell_agent` and starting registered agents. Treat it like an SSH private key. To
  rotate it, delete the file, run `--gen-api-key` again and restart.
- This mode serves no OAuth routes (`/register`, `/token`, `/login` or metadata).
- Behind a reverse proxy, `--public-url` must name the public URL, so the DNS-rebinding
  Host check accepts the proxied Host header.

Run it under systemd or any process manager. Behind nginx, proxy a location to the
loopback port with `proxy_http_version 1.1`, `proxy_buffering off` and a long
`proxy_read_timeout`, because responses stream over SSE.

#### Exposing it publicly

By default the server listens only on loopback. To expose it:

1. Put it behind TLS, with an nginx or Caddy vhost that proxies `https://swarm.example.com/`
   to `http://127.0.0.1:8765/`. Keep `--host 127.0.0.1`.
2. Start it with the public resource URL, so the metadata, audience checks and
   DNS-rebinding protection use the public host:
   `swarm-mcp --http --host 127.0.0.1 --port 8765 --public-url https://swarm.example.com/mcp`
   The `issuer_url` must use https. In builtin mode it defaults to `https://swarm.example.com`.
3. Set an admin password with `swarm auth set-password`, or switch to an external issuer.
4. Give clients that don't need to type into panes only `tangentswarm:read`.

## License

MIT
