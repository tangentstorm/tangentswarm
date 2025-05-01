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

1. Clone this repository
2. Make sure you have Python 3.10+ and tmux installed
3. Ensure dependencies are installed: `pip install PyYAML`
4. Make the script executable: `chmod +x swarm.py`
5. Add to your PATH (optional, for easier access)

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
- PyYAML
- Git

## License

MIT
