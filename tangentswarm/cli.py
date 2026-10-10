"""swarm: tmux-based launcher for per-branch work sessions (driven by ~/.swarm.yaml)."""
import os
import sys
import yaml
import subprocess
import time
import re
import shutil
from pathlib import Path

from . import tmux
from . import git

# Use ~/.swarm.yaml, or ./swarm.yaml if that does not exist
CONFIG_FILE = os.path.expanduser('~/.swarm.yaml')
LOCAL_CONFIG_FILE = 'swarm.yaml'

# Default programs to launch in the panes
DEFAULT_PROGRAMS = ['codex']

# Command sigils
SIGIL_NEW_WINDOW = '*'  # Create a new window
SIGIL_HORIZONTAL_SPLIT = '|'  # Split horizontally (side by side)
SIGIL_VERTICAL_SPLIT = '~'  # Split vertically (one above the other)
SIGIL_TMUX_COMMAND = '@'  # Run a tmux command against this session
SIGIL_TEMP_WINDOW = '!'  # Run command directly (not in tmux) and display output
VALID_SIGILS = [SIGIL_NEW_WINDOW, SIGIL_HORIZONTAL_SPLIT, SIGIL_VERTICAL_SPLIT, SIGIL_TMUX_COMMAND, SIGIL_TEMP_WINDOW]

def load_config():
    """Load configuration from YAML file.

    Checks for config in the following order:
    1. ~/.swarm.yaml (user's home directory)
    2. ./swarm.yaml (current directory)

    Returns a default config if neither file exists.
    """
    # First try the user's home directory config
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r') as f:
            return yaml.safe_load(f)

    # Then try the local directory config
    if os.path.exists(LOCAL_CONFIG_FILE):
        with open(LOCAL_CONFIG_FILE, 'r') as f:
            return yaml.safe_load(f)

    # If no config files found, return default config
    return {
        '.swarm': {
            'root': '.'  # Default to the current directory
        },
        'example_repo': {
            'branches': {
                'main': 5000
            },
            'programs': DEFAULT_PROGRAMS,
            'init': []
        }
    }

def save_config(config):
    """Save configuration to YAML file.

    Saves to ~/.swarm.yaml in the user's home directory, so swarm finds the
    same config from any directory.
    """
    # Make sure ~/.swarm.yaml is used for saving
    with open(CONFIG_FILE, 'w') as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)

def print_usage():
    print("Usage: swarm [<repo_name>] <branch_name>")
    print("       swarm -c status")
    print("")
    print("Agent / swarm tools (run in a control dir holding workers.jsonl, as in scialect):")
    print("       swarm -c local-status                 worker status table")
    print("       swarm -c tell-worker <worker> <verb> [arg]")
    print("       swarm -c step                         propose and run the next handoff")
    print("       swarm -c for-all '<cmd>'              run a command in every worker dir")
    print("       swarm -c agent-status <tmux-target>   detected agent + prompt state")
    print("       swarm -c tell-agent <tmux-target> <text...>")
    print("Cloud sessions and MCP auth:")
    print("       swarm cloud <login|list|status|open|wait|serve|orchestrator|client|sessions>")
    print("       swarm auth <set-password|approve|client|token|show-config>")
    print("MCP server: swarm-mcp (stdio) or swarm-mcp --http")


def _agent_status(argv):
    import json as _json
    from . import agents
    if len(argv) != 1:
        print("Usage: swarm -c agent-status <tmux-target>", file=sys.stderr)
        return 1
    print(_json.dumps(agents.agent_status(argv[0]), indent=2))
    return 0


def _tell_agent(argv):
    from . import agents
    if len(argv) < 2:
        print("Usage: swarm -c tell-agent <tmux-target> <text...>", file=sys.stderr)
        return 1
    try:
        agents.tell_agent(argv[0], ' '.join(argv[1:]))
    except agents.AgentNotReady as e:
        print(str(e), file=sys.stderr)
        return 1
    return 0


def run_subcommand(name, argv):
    """Run a newer subcommand (a scialect port, cloud or auth) and return its exit code."""
    if name == 'cloud':
        from .cloud import cli as cloud_cli
        return cloud_cli.main(argv)
    if name == 'auth':
        from . import auth_cli
        return auth_cli.main(argv)
    if name == 'local-status':
        from . import local_status
        return local_status.main(argv)
    if name == 'tell-worker':
        from . import tell_worker
        return tell_worker.main(argv)
    if name == 'for-all':
        from . import for_all
        return for_all.main(argv)
    if name == 'step':
        from . import local_step
        return local_step.main(argv)
    if name == 'agent-status':
        return _agent_status(argv)
    if name == 'tell-agent':
        return _tell_agent(argv)
    raise KeyError(name)


# `swarm cloud ...` and `swarm auth ...` are reserved words. Every other new command
# lives behind `-c`, so `swarm [<repo>] <branch>` keeps its meaning.
BARE_SUBCOMMANDS = ('cloud', 'auth')
DASH_C_SUBCOMMANDS = ('local-status', 'tell-worker', 'for-all', 'step', 'agent-status',
                      'tell-agent', 'cloud', 'auth')

def get_args():
    """Parse command line arguments and return command, repo_name, repo_url, and branch_name."""
    args = sys.argv[1:]
    config = load_config()

    if not config:
        print("No repositories configured.")
        sys.exit(1)

    # -h or --help prints usage and exits with status 0
    if len(args) == 1 and args[0] in ("-h", "--help"):
        print_usage()
        sys.exit(0)

    if len(args) == 2 and args[0] == "-c" and args[1] == "status":
        return "status", None, None, None

    if len(args) == 1:
        # Only a branch name was given, so use the first repo, skipping the .swarm entry
        branch_name = args[0]
        # Skip the .swarm entry
        repo_urls = [url for url in config.keys() if url != '.swarm']
        if not repo_urls:
            print("No repositories configured.")
            sys.exit(1)
        repo_url = repo_urls[0]
        repo_name = repo_url.split('/')[-1].split('.')[0]
        return "branch", repo_name, repo_url, branch_name
    elif len(args) == 2:
        repo_name = args[0]
        branch_name = args[1]

        repo_url = None
        for url in config:
            if url.endswith(repo_name) or repo_name in url:
                repo_url = url
                break

        if not repo_url:
            print(f"Error: Could not find URL for repo '{repo_name}' in config")
            sys.exit(1)

        return "branch", repo_name, repo_url, branch_name
    else:
        print_usage()
        sys.exit(1)

def session_exists(session_name):
    """Check if a tmux session exists."""
    return tmux.has_session(session_name)

def extract_sigil_and_command(command_str):
    """Extract the command sigil and the actual command from a command string.

    Returns:
        tuple: (sigil, command)
    """
    if not command_str:
        return (SIGIL_NEW_WINDOW, command_str)

    parts = command_str.split(' ', 1)

    if len(parts) == 2 and parts[0] in VALID_SIGILS:
        # A valid sigil followed by a command
        return (parts[0], parts[1])
    else:
        # No valid sigil, or nothing after the sigil
        return (SIGIL_NEW_WINDOW, command_str)

def create_tmux_session(session_name, branch_dir):
    """Create a new tmux session."""
    try:
        tmux.new_session(session_name, cwd=branch_dir, command=tmux.DEFAULT_SHELL)
    except tmux.TmuxError as e:
        print(f"Error creating session: {e.stderr}")
        return False

    # Name the window so it is easy to find
    tmux.rename_window(f'{session_name}:0', 'main')

    return True

def setup_and_run_programs(session_name, branch_dir, programs, port, env=None):
    """Set up the tmux session layout based on command sigils and run programs."""
    if not programs:
        return False

    if not create_tmux_session(session_name, branch_dir):
        return False

    current_window = 0
    current_pane = 0

    # Track whether we've used the initial window yet
    initial_window_used = False

    # Count the non-tmux commands at the start
    non_tmux_commands = 0
    for cmd in programs:
        sigil, _ = extract_sigil_and_command(cmd)
        if sigil in [SIGIL_TEMP_WINDOW, SIGIL_TMUX_COMMAND]:
            non_tmux_commands += 1
        else:
            break

    # Build export commands for any env variables
    env_exports = ""
    if env:
        for key, value in env.items():
            env_exports += f"export {key}=\"{value}\"; "

    for i, program in enumerate(programs):
        sigil, cmd = extract_sigil_and_command(program)

        cmd = replace_port_variables(cmd, port)

        # Prefix the command with the env exports
        if env_exports and sigil != SIGIL_TMUX_COMMAND and sigil != SIGIL_TEMP_WINDOW:
            cmd = f"{env_exports} {cmd}"

        # Commands that open no window (TEMP_WINDOW and TMUX_COMMAND)
        if sigil == SIGIL_TMUX_COMMAND:
            # Run tmux command against this session
            tmux.run_tmux_command(session_name, cmd)
            continue

        elif sigil == SIGIL_TEMP_WINDOW:
            # Run ! commands with subprocess instead of in tmux
            print(f"Running temporary command: {cmd}")
            try:
                # Use the current environment plus the configured variables
                env_dict = os.environ.copy()
                if env:
                    env_dict.update(env)

                # shell=True so the shell interprets the command
                result = subprocess.run(cmd, shell=True, cwd=branch_dir, env=env_dict,
                                       stderr=subprocess.PIPE, stdout=subprocess.PIPE, text=True)

                if result.stdout.strip():
                    print(f"Command output: {result.stdout.strip()}")

                if result.returncode != 0:
                    print(f"Command failed with exit code {result.returncode}: {result.stderr.strip()}")
            except Exception as e:
                print(f"Error executing command: {e}")
            continue

        # Window and pane commands use the initial window first
        if not initial_window_used:
            # This is the first window command, so it uses the initial window
            initial_window_used = True
            tmux.send_keys(f'{session_name}:{current_window}.{current_pane}', cmd)
            continue

        # Lay out the window or pane that the sigil asks for and run the command
        if sigil == SIGIL_NEW_WINDOW:
            # Create a new window with the next available index
            current_window += 1
            result = tmux.new_window_args('-t', session_name, '-c', branch_dir)
            if result.returncode == 0:
                current_pane = 0
                # Name the window after the first word of the command
                window_name = cmd.split()[0] if cmd else f"win{current_window}"
                tmux.rename_window(f'{session_name}:{current_window}', window_name)
                tmux.send_keys(f'{session_name}:{current_window}.{current_pane}', cmd)
            else:
                print(f"Failed to create new window: {result.stderr}")

        elif sigil == SIGIL_HORIZONTAL_SPLIT:
            # Create a horizontal split (side by side)
            result = tmux.split_window(f'{session_name}:{current_window}.{current_pane}', '-h', branch_dir)
            if result.returncode == 0:
                current_pane += 1
                tmux.send_keys(f'{session_name}:{current_window}.{current_pane}', cmd)
            else:
                print(f"Failed to create horizontal split: {result.stderr}")

        elif sigil == SIGIL_VERTICAL_SPLIT:
            # Create a vertical split (one above the other)
            result = tmux.split_window(f'{session_name}:{current_window}.{current_pane}', '-v', branch_dir)
            if result.returncode == 0:
                current_pane += 1
                tmux.send_keys(f'{session_name}:{current_window}.{current_pane}', cmd)
            else:
                print(f"Failed to create vertical split: {result.stderr}")

        elif sigil == SIGIL_TMUX_COMMAND:
            # Run tmux command against this session
            tmux.run_tmux_command(session_name, cmd)

    # Select the first pane of the first window
    tmux.select_pane(f'{session_name}:0.0')

    return True

def get_repo_env(config, repo_url):
    """Get environment dictionary from repo config."""
    if 'env' in config[repo_url]:
        return config[repo_url]['env']
    return {}

def get_branch_env(config, repo_url, branch_name):
    """Get environment dictionary from branch config."""
    branch_config = config[repo_url]['branches'][branch_name]

    # A branch config can be a dict with an 'env' key
    if isinstance(branch_config, dict) and 'env' in branch_config:
        return branch_config['env']
    return {}

def get_combined_env(config, repo_url, branch_name):
    """Combine repository and branch environment dictionaries.
    Branch environment values override repository environment values.
    """
    env = {}

    env.update(get_repo_env(config, repo_url))

    env.update(get_branch_env(config, repo_url, branch_name))

    return env

def get_programs(config, repo_url):
    """Get the list of programs to run from the config."""
    if 'programs' in config[repo_url]:
        return config[repo_url]['programs']
    return DEFAULT_PROGRAMS

def get_init_commands(config, repo_url):
    """Get the list of initialization commands to run from the config."""
    if 'init' in config[repo_url]:
        return config[repo_url]['init']
    return []

def replace_port_variables(command, port):
    """Replace ${PORT} and ${PORT+n} variables in a command string."""
    if not command:
        return command

    # Replace ${PORT} with the actual port
    command = command.replace('${PORT}', str(port))

    # Replace ${PORT+n} patterns
    pattern = r'\${PORT\+(\d+)}'
    matches = re.findall(pattern, command)

    for offset in matches:
        offset_value = int(offset)
        if offset_value <= 9:  # n is a single digit
            new_port = port + offset_value
            command = command.replace(f'${{PORT+{offset}}}', str(new_port))

    return command

def restart_session(session_name, branch_dir, programs, port, env=None):
    """Restart the session by killing it and creating a new one.

    main() does not call this. It stays for API compatibility.
    """
    max_attempts = 3
    attempt = 0

    # Kill the existing session with multiple attempts if needed
    while session_exists(session_name) and attempt < max_attempts:
        attempt += 1
        print(f"Killing session {session_name}... (attempt {attempt})")

        if attempt == 1:
            # First try normal kill-session
            subprocess.run(['tmux', 'kill-session', '-t', session_name], check=False)
        elif attempt == 2:
            # Second try, through the shell
            subprocess.run(['tmux', 'kill-session', '-t', session_name, '||', 'true'], shell=True, check=False)
        else:
            # As a last resort, kill the tmux server
            print("Warning: Using kill-server as last resort...")
            subprocess.run(['tmux', 'kill-server'], check=False)

        # Give tmux time to clean up
        time.sleep(1)

    if session_exists(session_name):
        print(f"Warning: Failed to kill session {session_name} after {max_attempts} attempts.")
        print("Proceeding anyway, but you may need to manually clean up tmux sessions.")

    # Wait before creating the new session
    time.sleep(0.5)

    # Create a new session with the configured layout and environment
    setup_and_run_programs(session_name, branch_dir, programs, port, env)
    return True  # Always return True. The result of setup_and_run_programs is not passed on.

# Chrome refuses to connect to these ports, so swarm never assigns them
CHROME_UNSAFE_PORTS = [5060, 5061] + list(range(6000, 6064))

def is_unsafe_port(port):
    """Check if a port is considered unsafe by Chrome."""
    return port in CHROME_UNSAFE_PORTS

def get_swarm_root(config):
    """Get the root directory for branch directories from the config.

    Uses .swarm.root from the configuration, or the current directory if it is not set.
    A leading ~ expands to the user's home directory.
    """
    if '.swarm' in config and 'root' in config['.swarm']:
        # Expand any ~ in the path to the user's home directory
        return os.path.expanduser(config['.swarm']['root'])
    return '.'  # Default to the current directory

def get_branch_port(config, repo_url, branch_name):
    """Extract port from branch configuration, which can be an integer or a dictionary with a 'port' key."""
    branch_config = config[repo_url]['branches'][branch_name]

    # branch_config can be a dict with a 'port' key
    if isinstance(branch_config, dict) and 'port' in branch_config:
        return branch_config['port']
    # Otherwise it is a plain port number
    return branch_config

def check_for_unsafe_ports(config):
    """Check configuration for any unsafe ports and warn the user.

    Returns:
        list: List of tuples (repo_url, branch_name, port) for each unsafe port found
    """
    unsafe_ports_found = []

    for repo_url, repo_config in config.items():
        if 'branches' in repo_config:
            for branch_name, branch_config in repo_config['branches'].items():
                # The port is an int or a dict with a 'port' key
                port = get_branch_port(config, repo_url, branch_name)

                if is_unsafe_port(port):
                    unsafe_ports_found.append((repo_url, branch_name, port))

    return unsafe_ports_found

def find_next_available_port(used_ports):
    """Find the next available port in the range 5000-6000 with gaps of 10."""
    # Every tenth port from 5000 to 6000, minus unsafe ports
    all_ports = [port for port in range(5000, 6001, 10) if not is_unsafe_port(port)]

    # Return the first one not in used_ports
    for port in all_ports:
        if port not in used_ports:
            return port

    # If every port is taken, fall back to 5000
    return 5000

def run_init_commands(branch_dir, init_commands, port, env=None):
    """Run initialization commands in the directory with port substitution and environment variables.

    Args:
        branch_dir: Directory where commands should be executed
        init_commands: List of commands to execute
        port: Port number for variable substitution
        env: Optional dictionary of environment variables

    Returns:
        bool: True if all commands succeeded, False otherwise.
    """
    if not init_commands:
        return True

    print("Running initialization commands...")
    for cmd in init_commands:
        processed_cmd = replace_port_variables(cmd, port)
        print(f"Executing: {processed_cmd}")

        # Environment for the subprocess
        env_dict = os.environ.copy()
        if env:
            env_dict.update(env)

        result = subprocess.run(processed_cmd, shell=True, cwd=branch_dir, env=env_dict)
        if result.returncode != 0:
            print(f"Error: Initialization command failed: '{processed_cmd}'")
            return False

    print("Initialization completed successfully.")
    return True

def branch_exists_on_remote(branch_dir, branch_name):
    """Check if a branch exists on the remote."""
    result = git.remote_branches('origin', branch_name, cwd=branch_dir)
    return bool(result.stdout.strip())

def configure_upstream(branch_dir, branch_name):
    """Configure branch's upstream without pushing."""
    print(f"Configuring branch '{branch_name}' to track origin/{branch_name} (tracking only, no push)")
    git.set_upstream_tracking(branch_name, 'origin', cwd=branch_dir)

def checkout_branch(branch_dir, branch_name):
    """Checkout the specified branch, handling various edge cases.

    Returns:
        bool: True if checkout was successful, False otherwise
    """
    print(f"Checking out branch '{branch_name}'...")

    local_branches = git.branch_list(cwd=branch_dir).stdout

    # Check if the branch exists locally
    branch_exists_locally = any(b.strip().replace('* ', '') == branch_name for b in local_branches.splitlines())

    if branch_exists_locally:
        # Checkout existing local branch
        result = git.checkout(branch_name, cwd=branch_dir)

        if result.returncode != 0:
            print(f"Error checking out local branch: {result.stderr}")
            return False

        print(f"Successfully checked out local branch '{branch_name}'")

        # Ensure tracking is set up
        setup_tracking(branch_dir, branch_name)
        return True

    # Check if branch exists on remote
    if branch_exists_on_remote(branch_dir, branch_name):
        # Branch exists on remote, create tracking branch
        result = git.checkout_track_branch(branch_name, f'origin/{branch_name}', cwd=branch_dir)

        if result.returncode != 0:
            print(f"Error creating tracking branch: {result.stderr}")
            return False

        print(f"Successfully created tracking branch for '{branch_name}'")
        return True

    # Branch doesn't exist locally or remotely, create new branch automatically
    print(f"Branch '{branch_name}' doesn't exist locally or remotely.")
    print(f"Creating new branch '{branch_name}' based on current branch")

    # Create new branch from current branch
    result = git.checkout_new_branch(branch_name, cwd=branch_dir)

    if result.returncode != 0:
        print(f"Error creating new branch: {result.stderr}")
        return False

    print(f"Successfully created new branch '{branch_name}'")

    # Configure branch to track origin/branch_name without pushing
    configure_upstream(branch_dir, branch_name)
    return True

def get_session_name(branch_name, branch_port, repo_name):
    """Generate the session name based on branch name.

    The format is port/branch_name. The 'main' branch uses the repo name
    in place of 'main'.

    Args:
        branch_name: Name of the branch
        branch_port: Port number assigned to the branch
        repo_name: Name of the repository

    Returns:
        str: The formatted session name
    """
    if branch_name == 'main':
        return f"{branch_port}/{repo_name}"
    else:
        return f"{branch_port}/{branch_name}"

def setup_tracking(branch_dir, branch_name):
    """Check if branch has tracking info, and if not, set it up."""
    # First check if tracking is already set up
    branch_info = git.branch_verbose(cwd=branch_dir).stdout

    # Tracking info appears in square brackets after the branch name
    pattern = re.compile(rf'[* ] {re.escape(branch_name)}\s+[0-9a-f]+ \[')
    tracking_set = bool(pattern.search(branch_info))

    if tracking_set:
        return

    # No tracking is set, so check whether the branch exists on the remote
    if branch_exists_on_remote(branch_dir, branch_name):
        # Set up tracking to origin/branch_name
        print(f"Setting upstream for branch '{branch_name}' to origin/{branch_name}")
        git.branch_set_upstream(branch_name, f'origin/{branch_name}', cwd=branch_dir)
    else:
        # The branch is not on the remote, so configure the upstream without pushing
        configure_upstream(branch_dir, branch_name)

def pull_branch(branch_dir, branch_name):
    """Pull latest changes for a branch, handling branches without tracking info."""
    print("Pulling latest changes...")

    # Ensure tracking is set up for the branch
    setup_tracking(branch_dir, branch_name)

    # Try a normal pull
    result = git.pull(cwd=branch_dir, ff_only=True)

    # Done if the pull worked
    if result.returncode == 0:
        if "Already up to date" in result.stdout:
            print("Already up to date.")
        else:
            print("Successfully pulled latest changes.")

def show_branch_status():
    """Show every configured branch, using its directory and .swarm-status file.
    The display has three groups:
    1. Inactive repositories and branches
    2. Active branches without tmux sessions
    3. Active branches with tmux sessions in a format similar to tmux switcher
    """
    config = load_config()

    inactive_repos = {}   # Structure: {repo_name: [branch_names]}
    active_no_tmux = []   # Structure: [{repo, branch, port, status}]
    active_tmux = []      # Structure: [{repo, branch, port, status, session_name}]

    active_tmux_sessions = []
    try:
        active_tmux_sessions = [s['name'] for s in tmux.list_sessions()]
    except Exception:
        # tmux may not be running
        pass

    root_dir = get_swarm_root(config)

    for repo_url, repo_config in config.items():
        # Skip the .swarm config entry
        if repo_url == '.swarm':
            continue

        repo_name = repo_url.split('/')[-1].split('.')[0]

        # Track if any branch in this repo is active
        repo_has_active_branch = False
        inactive_branches = []

        if 'branches' in repo_config:
            for branch_name in repo_config['branches'].keys():
                branch_dir = os.path.join(root_dir, f"{repo_name}.{branch_name}")

                if os.path.exists(branch_dir):
                    repo_has_active_branch = True
                    status = ""
                    swarm_status_file = f"{branch_dir}/.swarm-status"

                    # Check if .swarm-status file exists
                    if os.path.exists(swarm_status_file):
                        try:
                            with open(swarm_status_file, 'r') as f:
                                status = f.readline().strip()
                        except Exception:
                            # Ignore errors reading the file
                            pass

                    branch_config = repo_config['branches'][branch_name]
                    if isinstance(branch_config, dict) and 'port' in branch_config:
                        port = branch_config['port']
                    else:
                        port = branch_config

                    session_name = get_session_name(branch_name, port, repo_name)

                    if session_name in active_tmux_sessions:
                        # Active tmux session
                        active_tmux.append({
                            'repo': repo_name,
                            'branch': branch_name,
                            'port': port,
                            'status': status,
                            'session_name': session_name
                        })
                    else:
                        # No tmux session but directory exists
                        active_no_tmux.append({
                            'repo': repo_name,
                            'branch': branch_name,
                            'port': port,
                            'status': status
                        })
                else:
                    # Track inactive branches
                    inactive_branches.append(branch_name)

            # If repo has no active branches, add to inactive repos
            if not repo_has_active_branch:
                inactive_repos[repo_name] = list(repo_config['branches'].keys())
            elif inactive_branches:
                # If repo has some active and some inactive branches
                inactive_repos[repo_name] = inactive_branches

    # List inactive repositories and branches
    if inactive_repos:
        print("Inactive repositories and branches:")
        for repo, branches in sorted(inactive_repos.items()):
            print(f" - {repo}: {', '.join(sorted(branches))}")
        print()

    # Group active branches without tmux sessions by repo
    if active_no_tmux:
        print("Active branches without tmux sessions:")
        no_tmux_by_repo = {}

        # Group branches by repo
        for item in active_no_tmux:
            repo = item['repo']
            branch = item['branch']
            if repo not in no_tmux_by_repo:
                no_tmux_by_repo[repo] = []
            no_tmux_by_repo[repo].append(branch)

        # Print each repo and its branches
        for repo, branches in sorted(no_tmux_by_repo.items()):
            print(f" - {repo}: {', '.join(sorted(branches))}")
        print()

    # List all tmux sessions, including those not created by swarm
    all_tmux_sessions = []

    # First add our swarm sessions
    session_map = {}
    for item in active_tmux:
        session_name = item['session_name']
        all_tmux_sessions.append({
            'name': session_name,
            'status': item['status'],
            'session_name': session_name
        })
        session_map[session_name] = True

    # Then add any other tmux sessions not created by swarm
    for session in active_tmux_sessions:
        if session not in session_map:
            all_tmux_sessions.append({
                'name': session,
                'status': "",
                'session_name': session
            })

    # Display all tmux sessions
    if all_tmux_sessions:
        # Determine width for num column based on number of sessions
        num_width = len(str(len(all_tmux_sessions) - 1))
        num_width = max(num_width, 1)  # at least 1 character wide

        print("Active tmux sessions:")
        print()

        all_tmux_sessions.sort(key=lambda x: x['session_name'])

        # Get current session if we're in tmux
        current_session = None
        if 'TMUX' in os.environ:
            try:
                current_session_result = subprocess.run(
                    ['tmux', 'display-message', '-p', '#S'],
                    capture_output=True, text=True, check=False
                )
                if current_session_result.returncode == 0:
                    current_session = current_session_result.stdout.strip()
            except Exception:
                pass

        for i, item in enumerate(all_tmux_sessions):
            session_name = item['session_name']
            # Use " > " for current session, "   " (3 spaces) for others
            prefix = " > " if session_name == current_session else "   "
            print(f"{prefix}{i:<{num_width+1}} {item['name']:<25} {item['status']}")

        # Prompt for session selection
        if all_tmux_sessions:
            print("\nEnter session number to switch (or any other key to exit): ", end="", flush=True)
            try:
                # Read a single character without requiring Enter
                import tty
                import termios
                import sys

                fd = sys.stdin.fileno()
                old_settings = termios.tcgetattr(fd)
                try:
                    tty.setraw(fd)
                    ch = sys.stdin.read(1)
                finally:
                    termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

                idx = int(ch)
                if 0 <= idx < len(all_tmux_sessions):
                    session_name = all_tmux_sessions[idx]['session_name']
                    # Replace current process with tmux
                    if 'TMUX' in os.environ:
                        # Already in tmux, use switch-client
                        os.execvp('tmux', ['tmux', 'switch-client', '-t', session_name])
                    else:
                        # Not in tmux, attach to session
                        os.execvp('tmux', ['tmux', 'attach', '-t', session_name])
                else:
                    print(f"\nInvalid session number: {idx}")
            except (ValueError, IndexError):
                print("\nExiting session switcher.")
            except Exception as e:
                print(f"\nError: {e}")

def main():
    argv = sys.argv[1:]
    if argv and argv[0] in BARE_SUBCOMMANDS:
        sys.exit(run_subcommand(argv[0], argv[1:]))
    if len(argv) >= 2 and argv[0] == '-c' and argv[1] in DASH_C_SUBCOMMANDS:
        sys.exit(run_subcommand(argv[1], argv[2:]))

    command, repo_name, repo_url, branch_name = get_args()

    if command == "status":
        show_branch_status()
        return

    # Branch command mode
    if command == "branch":
        config = load_config()

        # Check for unsafe ports in the configuration
        unsafe_ports = check_for_unsafe_ports(config)
        if unsafe_ports:
            print("\nWARNING: Chrome considers the following ports unsafe and will block connections:")
            for repo_url, branch_name, port in unsafe_ports:
                print(f"  * Port {port} for branch '{branch_name}' in repo '{repo_url}'")
            print("These ports may cause issues with web services when accessed through Chrome.")
            print("Consider changing these ports in your swarm.yaml file.\n")

        # Ensure repo has a branches key
        if 'branches' not in config[repo_url]:
            config[repo_url] = {'branches': {}}

        # Ensure repo has a programs key
        if 'programs' not in config[repo_url]:
            config[repo_url]['programs'] = DEFAULT_PROGRAMS

        # Ensure repo has an init key
        if 'init' not in config[repo_url]:
            config[repo_url]['init'] = []

        # Ensure branch exists in repo config
        branch_port = None
        if branch_name not in config[repo_url]['branches']:
            used_ports = set()
            for repo_config in config.values():
                if 'branches' in repo_config:
                    for b_name, b_config in repo_config['branches'].items():
                        if isinstance(b_config, dict) and 'port' in b_config:
                            used_ports.add(b_config['port'])
                        else:
                            # Handle direct port assignment
                            used_ports.add(b_config)
                else:
                    # Legacy config format
                    used_ports.update(repo_config.values())

            # find_next_available_port skips unsafe ports
            port = find_next_available_port(used_ports)

            config[repo_url]['branches'][branch_name] = port
            save_config(config)
            branch_port = port
        else:
            branch_port = get_branch_port(config, repo_url, branch_name)

            # Check if this branch's port is unsafe
            if is_unsafe_port(branch_port):
                print(f"\nWARNING: The port {branch_port} assigned to branch '{branch_name}' is considered unsafe by Chrome.")
                print("Chrome will block connections to this port, which may cause issues with web services.")
                response = input("Would you like to reassign to a safe port? [Y/n]: ")

                if response.lower() not in ['n', 'no']:
                    # Collect all used ports except the current one
                    used_ports = set()
                    for r_url, r_config in config.items():
                        if 'branches' in r_config:
                            for b_name, b_config in r_config['branches'].items():
                                # Skip the current branch we're reassigning
                                if r_url == repo_url and b_name == branch_name:
                                    continue

                                # The port is an int or a dict with a 'port' key
                                if isinstance(b_config, dict) and 'port' in b_config:
                                    used_ports.add(b_config['port'])
                                else:
                                    used_ports.add(b_config)

                    # Find a new safe port
                    new_port = find_next_available_port(used_ports)
                    print(f"Reassigning port from {branch_port} to {new_port}")

                    # Update the config and keep any existing environment
                    branch_config = config[repo_url]['branches'][branch_name]
                    if isinstance(branch_config, dict):
                        branch_config['port'] = new_port
                    else:
                        # If it was a direct port assignment, replace with a dictionary
                        # that includes the port and an empty environment
                        config[repo_url]['branches'][branch_name] = {'port': new_port}

                    save_config(config)
                    branch_port = new_port

        programs = get_programs(config, repo_url)

        init_commands = get_init_commands(config, repo_url)

        combined_env = get_combined_env(config, repo_url, branch_name)

        root_dir = get_swarm_root(config)

        branch_dir = os.path.join(root_dir, f"{repo_name}.{branch_name}")
        branch_dir_path = Path(branch_dir).resolve()

        # Create directory if it doesn't exist
        is_new_repo = False
        if not os.path.exists(branch_dir):
            is_new_repo = True
            os.makedirs(branch_dir)

            print(f"Cloning repository {repo_url} into {branch_dir}")
            git.clone(repo_url, branch_dir)

            # Get the default branch that was checked out by the clone
            default_branch = git.branch_show_current(cwd=branch_dir).stdout.strip()
            print(f"Repository's default branch is: {default_branch}")

            # If default branch doesn't match requested branch, checkout the requested branch
            if default_branch != branch_name:
                print(f"Switching from default branch '{default_branch}' to requested branch '{branch_name}'")
                if not checkout_branch(branch_dir, branch_name):
                    response = input("Branch checkout failed. Continue with default branch? [y/N]: ")
                    if response.lower() != 'y':
                        print("Operation cancelled")
                        sys.exit(1)
            else:
                print(f"Default branch already matches requested branch: {branch_name}")
                # Ensure tracking is properly set up
                setup_tracking(branch_dir, branch_name)

            pull_branch(branch_dir, branch_name)
        else:
            # For existing repositories, check if current branch matches requested branch
            print(f"Using existing repository at {branch_dir}")

            current_branch = git.branch_show_current(cwd=branch_dir).stdout.strip()

            # Not on the requested branch, so ask the user what to do
            if current_branch != branch_name:
                print(f"Current branch is '{current_branch}', but requested branch is '{branch_name}'")
                response = input(f"Switch to '{branch_name}' branch? [Y/n]: ")

                if response.lower() not in ['n', 'no']:
                    # User wants to switch branches
                    print(f"Switching to '{branch_name}'")
                    if not checkout_branch(branch_dir, branch_name):
                        response = input("Branch checkout failed. Continue with current branch? [y/N]: ")
                        if response.lower() != 'y':
                            print("Operation cancelled")
                            sys.exit(1)
                    # Pull latest changes for the new branch
                    pull_branch(branch_dir, branch_name)
                else:
                    # User wants to stay on current branch
                    print(f"Keeping current branch: '{current_branch}'")
                    # Use the current branch name from here on, including for the session name
                    branch_name = current_branch
                    # Ensure tracking is properly set up
                    setup_tracking(branch_dir, branch_name)
                    # Pull latest changes for current branch
                    pull_branch(branch_dir, branch_name)
            else:
                print(f"Already on branch '{branch_name}'")
                # Already on the branch, but make sure tracking is set up
                setup_tracking(branch_dir, branch_name)
                pull_branch(branch_dir, branch_name)

        # Run initialization commands for new repositories
        if is_new_repo and init_commands:
            if not run_init_commands(branch_dir, init_commands, branch_port, combined_env):
                response = input("Initialization failed. Continue anyway? [y/N]: ")
                if response.lower() != 'y':
                    print("Operation cancelled")
                    sys.exit(1)

        session_name = get_session_name(branch_name, branch_port, repo_name)

        if session_exists(session_name):
            print(f"Session {session_name} already exists.")
            response = input("Restart session? [y/N]: ")
            if response.lower() == 'y':
                # Kill the session here instead of calling restart_session, which would run the setup twice
                max_attempts = 3
                attempt = 0

                # Kill the existing session with multiple attempts if needed
                while session_exists(session_name) and attempt < max_attempts:
                    attempt += 1
                    print(f"Killing session {session_name}... (attempt {attempt})")

                    if attempt == 1:
                        # First try normal kill-session
                        subprocess.run(['tmux', 'kill-session', '-t', session_name], check=False)
                    elif attempt == 2:
                        # Second try, through the shell
                        subprocess.run(['tmux', 'kill-session', '-t', session_name, '||', 'true'], shell=True, check=False)
                    else:
                        # As a last resort, kill the tmux server
                        print("Warning: Using kill-server as last resort...")
                        subprocess.run(['tmux', 'kill-server'], check=False)

                    # Give tmux time to clean up
                    time.sleep(1)

                if session_exists(session_name):
                    print(f"Warning: Failed to kill session {session_name} after {max_attempts} attempts.")
                    print("Proceeding anyway, but you may need to manually clean up tmux sessions.")

                # Wait before creating the new session
                time.sleep(0.5)

        # Create the session with the configured layout, either new or after killing the old one
        print(f"Creating tmux session: {session_name}")
        setup_and_run_programs(session_name, branch_dir, programs, branch_port, combined_env)

        # Check if we're already in a tmux session
        in_tmux = 'TMUX' in os.environ

        if in_tmux:
            print(f"Already in a tmux session, switching to session: {session_name}")
            # Use switch-client instead of attach when already in tmux
            tmux.switch_client(session_name)
            sys.exit(0)
        else:
            # Replace current process with tmux attach
            cmd = tmux.attach_session(session_name)
            os.execvp(cmd[0], cmd)

if __name__ == "__main__":
    main()