# TangentSwarm

TangentSwarm is a developer productivity tool that makes working with multiple branches in Git repositories easier,
by automating the creation of tmux sessions with customizable workspaces.

The intent is to assign each branch to a separate instance of an AI agent like Claude Code or OpenAI codex.

## Features

- Automatically clone repositories and check out branches
- Create consistent development environments with tmux
- Manage multiple branches across different repositories
- Configure custom programs to run in each pane
- Automatically assign ports to different branches
- Persist configuration between sessions

## Installation

1. Clone this repository
2. Make sure you have Python 3.6+ and tmux installed
3. Ensure dependencies are installed: `pip install PyYAML`
4. Make the script executable: `chmod +x swarm.py`
5. Add to your PATH (optional, for easier access)

## Usage

```bash
# Simple usage with default repository
./swarm.py <branch-name>

# Specify repository and branch
./swarm.py <repo-name> <branch-name>
```

## Configuration

Swarm uses a YAML configuration file (`swarm.yaml`) to store repository information, branch-to-port mappings, and programs to run.

Example configuration:

```yaml
git@github.com:username/repo:
  branches:
    main: 5000
    feature1: 5010
    feature2: 5020
  programs:
    - 'codex'
    - 'npm run dev'
    - 'python api/server.py'
```

### Configuration Fields

- `branches`: Maps branch names to port numbers (used for development servers)
- `programs`: List of commands to run in each tmux pane
  - First command: Left pane (usually editor/shell)
  - Second command: Right top pane (usually frontend server)
  - Third command: Right bottom pane (usually backend/API server)

If fewer than 3 programs are specified, `codex` will be used for the remaining panes.

## How It Works

1. Swarm checks if the requested branch exists in the configuration file
2. If not, it assigns a new port number and adds it to the configuration
3. It creates a directory for the repository/branch if it doesn't exist
4. It clones the repository and checks out the branch
5. It creates or updates a tmux session with three panes
6. It launches the configured programs in each pane
7. Finally, it attaches to the tmux session

## Default Setup

By default, Swarm creates a tmux session with:

1. Left pane: `codex` (or first program from config)
2. Right top pane: Second program from config (default: `codex`)
3. Right bottom pane: Third program from config (default: `codex`)

## Tips

- Customize the programs for each repository in the YAML config file
- Use branch-specific configurations when needed
- If already in a tmux session, Swarm will switch to the new session rather than nesting

## Requirements

- Python 3.6+
- tmux
- PyYAML
- Git

## License

MIT
