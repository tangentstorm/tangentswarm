#!/usr/bin/env python
import os
import sys
import yaml
import subprocess
import time
import re
import shutil
from pathlib import Path
import tmux
import git

# Use ~/.swarm.yaml as config file, fall back to local swarm.yaml if not found
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
            'root': '.'  # Default to current directory
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

    Saves to ~/.swarm.yaml in the user's home directory.
    This allows accessing the configuration from anywhere on the system.
    """
    # Make sure ~/.swarm.yaml is used for saving
    with open(CONFIG_FILE, 'w') as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)

def get_args():
    """Parse command line arguments and return command, repo_name, repo_url, and branch_name."""
    args = sys.argv[1:]
    config = load_config()

    if not config:
        print("No repositories configured.")
        sys.exit(1)

    # Handle the status command
    if len(args) == 2 and args[0] == "-c" and args[1] == "status":
        return "status", None, None, None

    # Handle regular branch commands
    if len(args) == 1:
        # Only branch name provided, use first repo (ignoring .swarm config entry)
        branch_name = args[0]
        # Filter out the .swarm config entry
        repo_urls = [url for url in config.keys() if url != '.swarm']
        if not repo_urls:
            print("No repositories configured.")
            sys.exit(1)
        repo_url = repo_urls[0]
        repo_name = repo_url.split('/')[-1].split('.')[0]
        return "branch", repo_name, repo_url, branch_name
    elif len(args) == 2:
        # Repo name and branch provided
        repo_name = args[0]
        branch_name = args[1]

        # Find the repo URL from config
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
        print("Usage: swarm.py [<repo_name>] <branch_name>")
        print("       swarm.py -c status")
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

    # Split the command to check for sigil prefix
    parts = command_str.split(' ', 1)

    # Use conditional logic instead of match statement
    if len(parts) == 2 and parts[0] in VALID_SIGILS:
        # Valid sigil found with command
        return (parts[0], parts[1])
    else:
        # No valid sigil or no command after sigil
        return (SIGIL_NEW_WINDOW, command_str)

def create_tmux_session(session_name, branch_dir):
    """Create a new tmux session."""
    # Create a new session with the shell
    result = tmux.new_session(session_name, branch_dir)
    if result.returncode != 0:
        print(f"Error creating session: {result.stderr}")
        return False

    # Rename the window to make it more recognizable
    tmux.rename_window(f'{session_name}:0', 'main')

    return True

def setup_and_run_programs(session_name, branch_dir, programs, port, env=None):
    """Set up the tmux session layout based on command sigils and run programs."""
    if not programs:
        return False

    # Create the initial session
    if not create_tmux_session(session_name, branch_dir):
        return False

    current_window = 0
    current_pane = 0

    # Track whether we've used the initial window yet
    initial_window_used = False

    # Count number of non-tmux commands at the beginning
    non_tmux_commands = 0
    for cmd in programs:
        sigil, _ = extract_sigil_and_command(cmd)
        if sigil in [SIGIL_TEMP_WINDOW, SIGIL_TMUX_COMMAND]:
            non_tmux_commands += 1
        else:
            break

    # Build environment export commands if env dictionary is provided
    env_exports = ""
    if env:
        for key, value in env.items():
            env_exports += f"export {key}=\"{value}\"; "

    # Process each program command with its sigil
    for i, program in enumerate(programs):
        sigil, cmd = extract_sigil_and_command(program)

        # Replace port variables in the command
        cmd = replace_port_variables(cmd, port)

        # Add environment variables to the command if available
        if env_exports and sigil != SIGIL_TMUX_COMMAND and sigil != SIGIL_TEMP_WINDOW:
            cmd = f"{env_exports} {cmd}"

        # Handle non-window commands (TEMP_WINDOW and TMUX_COMMAND)
        if sigil == SIGIL_TMUX_COMMAND:
            # Run tmux command against this session
            tmux.run_tmux_command(session_name, cmd)
            continue

        elif sigil == SIGIL_TEMP_WINDOW:
            # For temporary commands, run directly with subprocess instead of in tmux
            print(f"Running temporary command: {cmd}")
            try:
                # Build environment dictionary with current environment plus any specified vars
                env_dict = os.environ.copy()
                if env:
                    env_dict.update(env)

                # Run command directly with shell=True for proper shell interpretation
                result = subprocess.run(cmd, shell=True, cwd=branch_dir, env=env_dict,
                                       stderr=subprocess.PIPE, stdout=subprocess.PIPE, text=True)

                # Display output if any
                if result.stdout.strip():
                    print(f"Command output: {result.stdout.strip()}")

                # Check for errors
                if result.returncode != 0:
                    print(f"Command failed with exit code {result.returncode}: {result.stderr.strip()}")
            except Exception as e:
                print(f"Error executing command: {e}")
            continue

        # Handle window and pane commands - use initial window if possible
        if not initial_window_used:
            # This is the first window-based command - use the initial window
            initial_window_used = True
            tmux.send_keys(f'{session_name}:{current_window}.{current_pane}', cmd)
            continue

        # Apply the sigil and run the command based on the type
        if sigil == SIGIL_NEW_WINDOW:
            # Create a new window with the next available index
            current_window += 1
            result = tmux.new_window('-t', session_name, '-c', branch_dir)
            if result.returncode == 0:
                current_pane = 0
                # Rename the window based on the command (use first word)
                window_name = cmd.split()[0] if cmd else f"win{current_window}"
                tmux.rename_window(f'{session_name}:{current_window}', window_name)
                # Run the command in the new window
                tmux.send_keys(f'{session_name}:{current_window}.{current_pane}', cmd)
            else:
                print(f"Failed to create new window: {result.stderr}")

        elif sigil == SIGIL_HORIZONTAL_SPLIT:
            # Create a horizontal split (side by side)
            result = tmux.split_window(f'{session_name}:{current_window}.{current_pane}', '-h', branch_dir)
            if result.returncode == 0:
                current_pane += 1
                # Run the command in the new pane
                tmux.send_keys(f'{session_name}:{current_window}.{current_pane}', cmd)
            else:
                print(f"Failed to create horizontal split: {result.stderr}")

        elif sigil == SIGIL_VERTICAL_SPLIT:
            # Create a vertical split (one above the other)
            result = tmux.split_window(f'{session_name}:{current_window}.{current_pane}', '-v', branch_dir)
            if result.returncode == 0:
                current_pane += 1
                # Run the command in the new pane
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

    # If branch config is a dictionary with 'env' key
    if isinstance(branch_config, dict) and 'env' in branch_config:
        return branch_config['env']
    return {}

def get_combined_env(config, repo_url, branch_name):
    """Combine repository and branch environment dictionaries.
    Branch environment values override repository environment values.
    """
    env = {}

    # Start with repo env
    env.update(get_repo_env(config, repo_url))

    # Override with branch env
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

    # Find and replace ${PORT+n} patterns
    pattern = r'\${PORT\+(\d+)}'
    matches = re.findall(pattern, command)

    for offset in matches:
        offset_value = int(offset)
        if offset_value <= 9:  # Limit to single digits for simplicity
            new_port = port + offset_value
            command = command.replace(f'${{PORT+{offset}}}', str(new_port))

    return command

def restart_session(session_name, branch_dir, programs, port, env=None):
    """Restart the session by killing it and creating a new one.

    Note: This function is kept for API compatibility but is not used directly by main().
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
            # Second try with more direct approach
            subprocess.run(['tmux', 'kill-session', '-t', session_name, '||', 'true'], shell=True, check=False)
        else:
            # Last resort, kill tmux server (only do this if we're really stuck)
            print("Warning: Using kill-server as last resort...")
            subprocess.run(['tmux', 'kill-server'], check=False)

        # Add a delay to ensure tmux has time to clean up
        time.sleep(1)

    # Final verification
    if session_exists(session_name):
        print(f"Warning: Failed to kill session {session_name} after {max_attempts} attempts.")
        print("Proceeding anyway, but you may need to manually clean up tmux sessions.")

    # Wait a moment before creating the new session
    time.sleep(0.5)

    # Create a new session with the specified layout and environment
    setup_and_run_programs(session_name, branch_dir, programs, port, env)
    return True  # Return True to indicate success (doesn't propagate the result of setup_and_run_programs)

# Define Chrome's unsafe ports to avoid globally
CHROME_UNSAFE_PORTS = [5060, 5061] + list(range(6000, 6064))

def is_unsafe_port(port):
    """Check if a port is considered unsafe by Chrome."""
    return port in CHROME_UNSAFE_PORTS

def get_swarm_root(config):
    """Get the root directory for branch directories from the config.

    Looks for .swarm.root in the configuration. If not found, defaults to current directory.
    The root is expanded to handle ~ for the user's home directory.
    """
    if '.swarm' in config and 'root' in config['.swarm']:
        # Expand any ~ in the path to the user's home directory
        return os.path.expanduser(config['.swarm']['root'])
    return '.'  # Default to current directory

def get_branch_port(config, repo_url, branch_name):
    """Extract port from branch configuration, which can be an integer or a dictionary with a 'port' key."""
    branch_config = config[repo_url]['branches'][branch_name]

    # If branch_config is a dictionary with a 'port' key
    if isinstance(branch_config, dict) and 'port' in branch_config:
        return branch_config['port']
    # Otherwise, assume it's a direct port number
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
                # Extract port depending on the format (integer or dictionary)
                port = get_branch_port(config, repo_url, branch_name)

                if is_unsafe_port(port):
                    unsafe_ports_found.append((repo_url, branch_name, port))

    return unsafe_ports_found

def find_next_available_port(used_ports):
    """Find the next available port in the range 5000-6000 with gaps of 10."""
    # Generate all possible ports in the range with gaps of 10, excluding unsafe ports
    all_ports = [port for port in range(5000, 6001, 10) if not is_unsafe_port(port)]

    # Find the first available port that's not in used_ports
    for port in all_ports:
        if port not in used_ports:
            return port

    # If all ports are used, start over from 5000 (shouldn't happen with this range)
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
        # Replace port variables in the command
        processed_cmd = replace_port_variables(cmd, port)
        print(f"Executing: {processed_cmd}")

        # Set up environment for subprocess
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

    # First check if branch exists locally
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

    Use consistent format: port/branch_name for all branches
    For 'main' branch, use the repo_name instead of 'main'

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

    # Look for branch name with tracking info (inside square brackets)
    pattern = re.compile(rf'[* ] {re.escape(branch_name)}\s+[0-9a-f]+ \[')
    tracking_set = bool(pattern.search(branch_info))

    if tracking_set:
        return

    # If no tracking is set, check if the branch exists on remote
    if branch_exists_on_remote(branch_dir, branch_name):
        # Set up tracking to origin/branch_name
        print(f"Setting upstream for branch '{branch_name}' to origin/{branch_name}")
        git.branch_set_upstream(branch_name, f'origin/{branch_name}', cwd=branch_dir)
    else:
        # Branch doesn't exist on remote, just configure upstream without pushing
        configure_upstream(branch_dir, branch_name)

def pull_branch(branch_dir, branch_name):
    """Pull latest changes for a branch, handling branches without tracking info."""
    print("Pulling latest changes...")

    # Ensure tracking is set up for the branch
    setup_tracking(branch_dir, branch_name)

    # Try a normal pull
    result = git.pull(cwd=branch_dir, ff_only=True)

    # If successful, we're done
    if result.returncode == 0:
        if "Already up to date" in result.stdout:
            print("Already up to date.")
        else:
            print("Successfully pulled latest changes.")

def show_branch_status():
    """Loop through all branches defined in the config, check directories and .swarm status files.
    Display three categories:
    1. Inactive repositories and branches
    2. Active branches without tmux sessions
    3. Active branches with tmux sessions in a format similar to tmux switcher
    """
    config = load_config()

    # Track different categories
    inactive_repos = {}   # Structure: {repo_name: [branch_names]}
    active_no_tmux = []   # Structure: [{repo, branch, port, status}]
    active_tmux = []      # Structure: [{repo, branch, port, status, session_name}]

    # Get current tmux sessions
    active_tmux_sessions = []
    try:
        tmux_ls_result = tmux.list_sessions()

        if tmux_ls_result.returncode == 0:
            # Parse the tmux ls output to get session names
            lines = tmux_ls_result.stdout.strip().split('\n')
            for line in lines:
                if line:
                    # Extract session name (everything before the colon)
                    session_name = line.split(':')[0]
                    active_tmux_sessions.append(session_name)
    except Exception:
        # Silently handle the case where tmux is not running
        pass

    # Get root directory from config
    root_dir = get_swarm_root(config)

    # Go through each repo in the config
    for repo_url, repo_config in config.items():
        # Skip the .swarm config entry
        if repo_url == '.swarm':
            continue

        repo_name = repo_url.split('/')[-1].split('.')[0]

        # Track if any branch in this repo is active
        repo_has_active_branch = False
        inactive_branches = []

        # Go through each branch in the repo
        if 'branches' in repo_config:
            for branch_name in repo_config['branches'].keys():
                branch_dir = os.path.join(root_dir, f"{repo_name}.{branch_name}")

                # Check if the directory exists
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
                            # Silently ignore errors reading the file
                            pass

                    # Get branch port
                    branch_config = repo_config['branches'][branch_name]
                    if isinstance(branch_config, dict) and 'port' in branch_config:
                        port = branch_config['port']
                    else:
                        port = branch_config

                    # Get session name
                    session_name = get_session_name(branch_name, port, repo_name)

                    # Check if session exists in tmux
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
        num_width = max(num_width, 1)  # At least 1 char wide

        print("Active tmux sessions:")
        print()

        # Sort by session name
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

                # Try to convert to integer
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
    # Load arguments
    command, repo_name, repo_url, branch_name = get_args()

    # Handle status command
    if command == "status":
        show_branch_status()
        return

    # Branch command mode - the original behavior
    if command == "branch":
        # Load config
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
            # Collect all used ports
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
                    # Handle legacy config format for backwards compatibility
                    used_ports.update(repo_config.values())

            # Find next available port (this will automatically avoid unsafe ports)
            port = find_next_available_port(used_ports)

            # Add new branch with port
            config[repo_url]['branches'][branch_name] = port
            save_config(config)
            branch_port = port
        else:
            # Use existing port for this branch
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

                                # Extract port depending on format
                                if isinstance(b_config, dict) and 'port' in b_config:
                                    used_ports.add(b_config['port'])
                                else:
                                    used_ports.add(b_config)

                    # Find a new safe port
                    new_port = find_next_available_port(used_ports)
                    print(f"Reassigning port from {branch_port} to {new_port}")

                    # Update config - preserve environment if it exists
                    branch_config = config[repo_url]['branches'][branch_name]
                    if isinstance(branch_config, dict):
                        branch_config['port'] = new_port
                    else:
                        # If it was a direct port assignment, replace with a dictionary
                        # that includes the port and an empty environment
                        config[repo_url]['branches'][branch_name] = {'port': new_port}

                    save_config(config)
                    branch_port = new_port

        # Get programs to launch
        programs = get_programs(config, repo_url)

        # Get initialization commands
        init_commands = get_init_commands(config, repo_url)

        # Get combined environment variables
        combined_env = get_combined_env(config, repo_url, branch_name)

        # Get root directory from config
        root_dir = get_swarm_root(config)

        # Checkout directory
        branch_dir = os.path.join(root_dir, f"{repo_name}.{branch_name}")
        branch_dir_path = Path(branch_dir).resolve()

        # Create directory if it doesn't exist
        is_new_repo = False
        if not os.path.exists(branch_dir):
            is_new_repo = True
            os.makedirs(branch_dir)

            # Clone repo
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

            # Pull latest changes
            pull_branch(branch_dir, branch_name)
        else:
            # For existing repositories, check if current branch matches requested branch
            print(f"Using existing repository at {branch_dir}")

            # Get current branch
            current_branch = git.branch_show_current(cwd=branch_dir).stdout.strip()

            # If we're not on the requested branch, ask the user what to do
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
                    # Use the current branch name instead of requested branch
                    # for all subsequent operations including session naming
                    branch_name = current_branch
                    # Ensure tracking is properly set up
                    setup_tracking(branch_dir, branch_name)
                    # Pull latest changes for current branch
                    pull_branch(branch_dir, branch_name)
            else:
                print(f"Already on branch '{branch_name}'")
                # Even if already on the branch, ensure tracking is properly set up
                setup_tracking(branch_dir, branch_name)
                # Pull latest changes
                pull_branch(branch_dir, branch_name)

        # Run initialization commands for new repositories
        if is_new_repo and init_commands:
            if not run_init_commands(branch_dir, init_commands, branch_port, combined_env):
                response = input("Initialization failed. Continue anyway? [y/N]: ")
                if response.lower() != 'y':
                    print("Operation cancelled")
                    sys.exit(1)

        # Generate the session name using the helper function
        session_name = get_session_name(branch_name, branch_port, repo_name)

        # Check if the session already exists
        if session_exists(session_name):
            print(f"Session {session_name} already exists.")
            response = input("Restart session? [y/N]: ")
            if response.lower() == 'y':
                # Don't call restart_session - we'll handle it directly to avoid double setup
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
                        # Second try with more direct approach
                        subprocess.run(['tmux', 'kill-session', '-t', session_name, '||', 'true'], shell=True, check=False)
                    else:
                        # Last resort, kill tmux server (only do this if we're really stuck)
                        print("Warning: Using kill-server as last resort...")
                        subprocess.run(['tmux', 'kill-server'], check=False)

                    # Add a delay to ensure tmux has time to clean up
                    time.sleep(1)

                # Final verification
                if session_exists(session_name):
                    print(f"Warning: Failed to kill session {session_name} after {max_attempts} attempts.")
                    print("Proceeding anyway, but you may need to manually clean up tmux sessions.")

                # Wait a moment before creating the new session
                time.sleep(0.5)

        # Create/recreate session with the specified layout
        # This will be used both for new sessions and after killing existing ones
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