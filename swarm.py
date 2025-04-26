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
import git

CONFIG_FILE = 'swarm.yaml'

# Default programs to launch in the panes
DEFAULT_PROGRAMS = ['codex']

# Command sigils
SIGIL_NEW_WINDOW = '*'  # Create a new window
SIGIL_HORIZONTAL_SPLIT = '|'  # Split horizontally (side by side)
SIGIL_VERTICAL_SPLIT = '~'  # Split vertically (one above the other)
SIGIL_TMUX_COMMAND = '@'  # Run a tmux command against this session
SIGIL_NO_SHELL = '!'  # Run command without a shell (direct execution)
VALID_SIGILS = [SIGIL_NEW_WINDOW, SIGIL_HORIZONTAL_SPLIT, SIGIL_VERTICAL_SPLIT, SIGIL_TMUX_COMMAND, SIGIL_NO_SHELL]

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
    """Set up the tmux session layout based on command sigils and run programs."""
    if not programs:
        return False

    # Create the initial session
    if not create_tmux_session(session_name, branch_dir):
        return False

    current_window = 0
    current_pane = 0

    # Process each program command with its sigil
    for i, program in enumerate(programs):
        sigil, cmd = extract_sigil_and_command(program)

        # Replace port variables in the command
        cmd = replace_port_variables(cmd, port)

        # Handle first command specially
        if i == 0:
            if sigil == SIGIL_TMUX_COMMAND:
                # Run tmux command against this session
                tmux.run_tmux_command(session_name, cmd)
            elif sigil == SIGIL_NO_SHELL:
                # Create a temporary window to run the command
                temp_win = f"{session_name}:temp"
                tmux.new_window('-t', session_name, '-n', 'temp')
                # Run the command directly without a shell
                subprocess.run(cmd.split(), cwd=branch_dir)
                # Kill the temporary window when done
                tmux.kill_pane('-t', temp_win)
            else:
                # Regular command with shell in initial pane
                tmux.send_keys(f'{session_name}:{current_window}.{current_pane}', cmd)
            continue

        # Apply the sigil and run the command based on the type
        if sigil == SIGIL_NEW_WINDOW:
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

        elif sigil == SIGIL_NO_SHELL:
            # Create a temporary window to run the command
            temp_win = f"{session_name}:temp"
            tmux.new_window('-t', session_name, '-n', 'temp')
            # Run the command directly without a shell
            subprocess.run(cmd.split(), cwd=branch_dir)
            # Kill the temporary window when done
            tmux.kill_pane('-t', temp_win)

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

        # Clone repo
        print(f"Cloning repository {repo_url} into {branch_dir}")
        git.clone(repo_url, branch_dir)

        # Checkout the requested branch
        if not checkout_branch(branch_dir, branch_name):
            response = input("Branch checkout failed. Continue anyway? [y/N]: ")
            if response.lower() != 'y':
                print("Operation cancelled")
                sys.exit(1)

        # Pull latest changes
        pull_branch(branch_dir, branch_name)
    else:
        # For existing repositories, make sure we checkout the correct branch
        print(f"Using existing repository at {branch_dir}")

        # Get current branch
        current_branch = git.branch_show_current(cwd=branch_dir).stdout.strip()

        # If we're not on the requested branch, checkout it out
        if current_branch != branch_name:
            print(f"Current branch is '{current_branch}', switching to '{branch_name}'")
            if not checkout_branch(branch_dir, branch_name):
                response = input("Branch checkout failed. Continue with current branch? [y/N]: ")
                if response.lower() != 'y':
                    print("Operation cancelled")
                    sys.exit(1)
        else:
            print(f"Already on branch '{branch_name}'")
            # Even if already on the branch, ensure tracking is properly set up
            setup_tracking(branch_dir, branch_name)

        # Always pull the latest changes
        pull_branch(branch_dir, branch_name)

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