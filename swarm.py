#!/usr/bin/env python3
import os
import sys
import yaml
import subprocess
import time
import re
import shutil
from pathlib import Path
import tmux

CONFIG_FILE = 'swarm.yaml'

# Default programs to launch in the panes
DEFAULT_PROGRAMS = ['codex']

# Layout prefixes
LAYOUT_NEW_WINDOW = '*'  # Create a new window
LAYOUT_HORIZONTAL_SPLIT = '|'  # Split horizontally (side by side)
LAYOUT_VERTICAL_SPLIT = '~'  # Split vertically (one above the other)
VALID_LAYOUTS = [LAYOUT_NEW_WINDOW, LAYOUT_HORIZONTAL_SPLIT, LAYOUT_VERTICAL_SPLIT]

def load_config():
    """Load configuration from YAML file."""
    if not os.path.exists(CONFIG_FILE):
        # Default config with an example repo and branches
        return {
            'example_repo': {
                'branches': {
                    'main': 5000
                },
                'programs': DEFAULT_PROGRAMS,
                'init': []
            }
        }

    with open(CONFIG_FILE, 'r') as f:
        return yaml.safe_load(f)

def save_config(config):
    """Save configuration to YAML file."""
    with open(CONFIG_FILE, 'w') as f:
        yaml.dump(config, f, default_flow_style=False)

def get_args():
    """Parse command line arguments and return repo_name, repo_url, and branch_name."""
    args = sys.argv[1:]
    config = load_config()

    if not config:
        print("No repositories configured.")
        sys.exit(1)

    if len(args) == 1:
        # Only branch name provided, use first repo
        branch_name = args[0]
        repo_url = list(config.keys())[0]
        repo_name = repo_url.split('/')[-1].split('.')[0]
        return repo_name, repo_url, branch_name
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

        return repo_name, repo_url, branch_name
    else:
        print("Usage: swarm.py [<repo_name>] <branch_name>")
        sys.exit(1)

def session_exists(session_name):
    """Check if a tmux session exists."""
    return tmux.has_session(session_name)

def extract_layout_and_command(command_str):
    """Extract the layout prefix and the actual command from a command string.

    Returns:
        tuple: (layout_prefix, command)
    """
    if not command_str:
        return (LAYOUT_NEW_WINDOW, command_str)

    # Check if the command starts with a valid layout prefix followed by a space
    parts = command_str.split(' ', 1)
    if len(parts) > 1 and parts[0] in VALID_LAYOUTS:
        layout = parts[0]
        cmd = parts[1]
        return (layout, cmd)

    # Default to new window if no valid prefix is found
    return (LAYOUT_NEW_WINDOW, command_str)

def create_tmux_session(session_name, branch_dir):
    """Create a new tmux session."""
    print(f"Creating new tmux session: {session_name}")

    # Create a new session with the shell
    result = tmux.new_session(session_name, branch_dir)
    if result.returncode != 0:
        print(f"Error creating session: {result.stderr}")
        return False

    # Rename the window to make it more recognizable
    tmux.rename_window(f'{session_name}:0', 'main')

    return True

def setup_and_run_programs(session_name, branch_dir, programs, port):
    """Set up the tmux session layout based on command prefixes and run programs."""
    if not programs:
        return False

    # Create the initial session
    if not create_tmux_session(session_name, branch_dir):
        return False

    current_window = 0
    current_pane = 0

    # Process each program command with its layout
    for i, program in enumerate(programs):
        layout, cmd = extract_layout_and_command(program)

        # Replace port variables in the command
        cmd = replace_port_variables(cmd, port)

        if i == 0:
            # First command always runs in the initial pane
            tmux.send_keys(f'{session_name}:{current_window}.{current_pane}', cmd)
            continue

        # Apply the layout and run the command
        if layout == LAYOUT_NEW_WINDOW:
            # Create a new window
            result = tmux.new_window('-t', f'{session_name}:{current_window+1}', '-c', branch_dir)
            if result.returncode == 0:
                current_window += 1
                current_pane = 0
                # Rename the window based on the command (use first word)
                window_name = cmd.split()[0] if cmd else f"win{current_window}"
                tmux.rename_window(f'{session_name}:{current_window}', window_name)
                # Run the command in the new window
                tmux.send_keys(f'{session_name}:{current_window}.{current_pane}', cmd)
            else:
                print(f"Failed to create new window: {result.stderr}")

        elif layout == LAYOUT_HORIZONTAL_SPLIT:
            # Create a horizontal split (side by side)
            result = tmux.split_window(f'{session_name}:{current_window}.{current_pane}', '-h', branch_dir)
            if result.returncode == 0:
                current_pane += 1
                # Run the command in the new pane
                tmux.send_keys(f'{session_name}:{current_window}.{current_pane}', cmd)
            else:
                print(f"Failed to create horizontal split: {result.stderr}")

        elif layout == LAYOUT_VERTICAL_SPLIT:
            # Create a vertical split (one above the other)
            result = tmux.split_window(f'{session_name}:{current_window}.{current_pane}', '-v', branch_dir)
            if result.returncode == 0:
                current_pane += 1
                # Run the command in the new pane
                tmux.send_keys(f'{session_name}:{current_window}.{current_pane}', cmd)
            else:
                print(f"Failed to create vertical split: {result.stderr}")

    # Select the first pane of the first window
    tmux.select_pane(f'{session_name}:0.0')

    return True

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

def restart_session(session_name, branch_dir, programs, port):
    """Restart the session by killing it and creating a new one."""
    # Kill the existing session
    if session_exists(session_name):
        tmux.kill_session('-t', session_name)

    # Create a new session with the specified layout
    return setup_and_run_programs(session_name, branch_dir, programs, port)

def find_next_available_port(used_ports):
    """Find the next available port in the range 5000-6000 with gaps of 10."""
    # Generate all possible ports in the range with gaps of 10
    all_ports = list(range(5000, 6001, 10))

    # Find the first available port that's not in used_ports
    for port in all_ports:
        if port not in used_ports:
            return port

    # If all ports are used, start over from 5000 (shouldn't happen with this range)
    return 5000

def run_init_commands(branch_dir, init_commands, port):
    """Run initialization commands in the directory with port substitution.

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
        result = subprocess.run(processed_cmd, shell=True, cwd=branch_dir)
        if result.returncode != 0:
            print(f"Error: Initialization command failed: '{processed_cmd}'")
            return False

    print("Initialization completed successfully.")
    return True

def main():
    # Load arguments
    repo_name, repo_url, branch_name = get_args()

    # Load config
    config = load_config()

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
                used_ports.update(repo_config['branches'].values())
            else:
                # Handle legacy config format for backwards compatibility
                used_ports.update(repo_config.values())

        # Find next available port
        port = find_next_available_port(used_ports)

        # Add new branch with port
        config[repo_url]['branches'][branch_name] = port
        save_config(config)
        branch_port = port
    else:
        # Use existing port for this branch
        branch_port = config[repo_url]['branches'][branch_name]

    # Get programs to launch
    programs = get_programs(config, repo_url)

    # Get initialization commands
    init_commands = get_init_commands(config, repo_url)

    # Checkout directory
    branch_dir = f"./{repo_name}.{branch_name}"
    branch_dir_path = Path(branch_dir).resolve()

    # Create directory if it doesn't exist
    is_new_repo = False
    if not os.path.exists(branch_dir):
        is_new_repo = True
        os.makedirs(branch_dir)

        # Clone repo and checkout branch
        subprocess.run(['git', 'clone', repo_url, branch_dir], check=True)

        # Try to checkout with -b to create a new branch and set upstream tracking in one command
        result = subprocess.run(['git', 'checkout', '-b', branch_name, '--track', f'origin/{branch_name}'],
                               cwd=branch_dir, stderr=subprocess.PIPE, stdout=subprocess.PIPE, text=True, check=False)

        # If branch already exists, warn and ask user
        if result.returncode != 0 and 'already exists' in result.stderr:
            print(f"Warning: Branch '{branch_name}' already exists")
            response = input("Continue with existing branch? [y/N]: ")
            if response.lower() != 'y':
                print("Operation cancelled")
                sys.exit(1)

            # Checkout existing branch instead
            subprocess.run(['git', 'checkout', branch_name], cwd=branch_dir, check=True)

        subprocess.run(['git', 'pull'], cwd=branch_dir, check=False)

    # Run initialization commands for new repositories
    if is_new_repo and init_commands:
        if not run_init_commands(branch_dir, init_commands, branch_port):
            response = input("Initialization failed. Continue anyway? [y/N]: ")
            if response.lower() != 'y':
                print("Operation cancelled")
                sys.exit(1)

    # Session name includes the port number: PORT/branch_name
    session_name = f"{branch_port}/{branch_name}"

    # Check if the session already exists
    if session_exists(session_name):
        print(f"Session {session_name} already exists.")
        response = input("Restart session? [y/N]: ")
        if response.lower() == 'y':
            restart_session(session_name, branch_dir, programs, branch_port)
    else:
        # Create a new session with the specified layout
        setup_and_run_programs(session_name, branch_dir, programs, branch_port)

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