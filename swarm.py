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

def load_config():
    """Load configuration from YAML file."""
    if not os.path.exists(CONFIG_FILE):
        # Default config with an example repo and branches
        return {
            'example_repo': {
                'branches': {
                    'main': 5000
                },
                'programs': DEFAULT_PROGRAMS
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

def setup_tmux_session(branch_dir, session_name):
    """Create a new tmux session and set up the window layout."""
    print(f"Creating new tmux session: {session_name}")

    # Create a new session with the shell
    result1 = tmux.new_session(session_name, branch_dir)
    if result1.returncode != 0:
        print(f"Error creating session: {result1.stderr}")
        return False

    # Rename the window to 'codex'
    tmux.rename_window(f'{session_name}:0', 'codex')

    # List panes to debug
    panes_result = tmux.list_panes(session_name)
    print(f"Initial panes: {panes_result.stdout}")

    # Add a small delay to ensure the session is fully initialized
    time.sleep(1)

    # Create second window pane (use explicit target for first pane)
    result2 = tmux.split_window(f'{session_name}:0.0', '-h', branch_dir)
    if result2.returncode != 0:
        print(f"Error creating second pane: {result2.stderr}")
        # Try to create the pane again with just the session name
        result2b = tmux.split_window(session_name, '-h', branch_dir)
        if result2b.returncode != 0:
            print(f"Second attempt failed: {result2b.stderr}")
            return False

    # List panes to debug after first split
    panes_result2 = tmux.list_panes(session_name, '#{pane_index}')
    print(f"Panes after first split: {panes_result2.stdout}")

    # Add a small delay between pane creations
    time.sleep(1)

    # For the third pane, check if pane 1 exists, otherwise use session name
    if '1' in panes_result2.stdout:
        target = f'{session_name}.1'
    else:
        # Just use the session name and let tmux figure it out
        target = session_name

    # Try all possible pane targets for the third pane
    result3 = tmux.split_window(target, '-v', branch_dir)
    if result3.returncode != 0:
        print(f"First vertical split attempt failed, trying left pane: {result3.stderr}")
        result3b = tmux.split_window(f'{session_name}:0.0', '-v', branch_dir)

        if result3b.returncode != 0:
            print(f"Second vertical split attempt failed, trying session: {result3b.stderr}")
            # Try with just the session name
            result3c = tmux.split_window(session_name, '-v', branch_dir)

            if result3c.returncode != 0:
                print(f"All vertical split attempts failed: {result3c.stderr}")
                return False

    # Select the left pane (codex)
    tmux.select_pane(f'{session_name}:0.0')
    return True

def get_programs(config, repo_url):
    """Get the list of programs to run from the config."""
    if 'programs' in config[repo_url]:
        return config[repo_url]['programs']
    return DEFAULT_PROGRAMS

def launch_programs(session_name, programs):
    """Launch programs in the tmux panes."""
    # Check if we have enough programs defined
    if len(programs) < 3:
        print(f"Warning: Only {len(programs)} programs defined. Using defaults for missing programs.")
        # Use defaults for missing ones - just run codex in all panes
        programs_to_run = programs.copy()
        while len(programs_to_run) < 3:
            programs_to_run.append('codex')
    else:
        programs_to_run = programs[:3]

    # Launch each program in its respective pane
    for i, program in enumerate(programs_to_run):
        tmux.send_keys(f'{session_name}:0.{i}', program)

def restart_programs(session_name, programs):
    """Restart programs in the tmux panes."""
    # Check if we have enough programs defined
    if len(programs) < 3:
        print(f"Warning: Only {len(programs)} programs defined. Using defaults for missing programs.")
        # Use defaults for missing ones - just run codex in all panes
        programs_to_run = programs.copy()
        while len(programs_to_run) < 3:
            programs_to_run.append('codex')
    else:
        programs_to_run = programs[:3]

    # Restart each program in its respective pane
    for i, program in enumerate(programs_to_run):
        tmux.send_keys(f'{session_name}:0.{i}', 'C-c', False)
        tmux.send_keys(f'{session_name}:0.{i}', 'clear')
        tmux.send_keys(f'{session_name}:0.{i}', program)

def reset_session_layout(session_name, branch_dir):
    """Reset the session layout to ensure it has exactly three panes."""
    # First check if the session has the right number of panes
    result = tmux.list_panes(session_name, '#{pane_index}')
    panes = result.stdout.strip().split('\n')

    # Kill existing panes if layout is wrong
    if len(panes) != 3:
        # Kill all panes except the first one
        for pane in panes[1:]:
            try:
                tmux.kill_pane(f'{session_name}.{pane}')
            except Exception:
                pass

        # List panes to debug
        panes_result = tmux.list_panes(session_name, '#{pane_index}')
        print(f"Initial panes (update): {panes_result.stdout}")

        # Add a small delay to ensure commands complete
        time.sleep(1)

        # Add panes for vite and API
        result2 = tmux.split_window(f'{session_name}:0.0', '-h', branch_dir)
        if result2.returncode != 0:
            print(f"Error creating vite pane (update): {result2.stderr}")
            # Try to create the pane again with just the session name
            result2b = tmux.split_window(session_name, '-h', branch_dir)
            if result2b.returncode != 0:
                print(f"Second attempt failed (update): {result2b.stderr}")
                return False

        # List panes to debug after first split
        panes_result2 = tmux.list_panes(session_name, '#{pane_index}')
        print(f"Panes after first split (update): {panes_result2.stdout}")

        # Add a small delay between pane creations
        time.sleep(1)

        # For the third pane, check if pane 1 exists, otherwise use session name
        if '1' in panes_result2.stdout:
            target = f'{session_name}.1'
        else:
            # Just use the session name and let tmux figure it out
            target = session_name

        # Try all possible pane targets for the third pane
        result3 = tmux.split_window(target, '-v', branch_dir)
        if result3.returncode != 0:
            print(f"First vertical split attempt failed (update), trying left pane: {result3.stderr}")
            result3b = tmux.split_window(f'{session_name}:0.0', '-v', branch_dir)

            if result3b.returncode != 0:
                print(f"Second vertical split attempt failed (update), trying session: {result3b.stderr}")
                # Try with just the session name
                result3c = tmux.split_window(session_name, '-v', branch_dir)

                if result3c.returncode != 0:
                    print(f"All vertical split attempts failed (update): {result3c.stderr}")
                    return False

    # Select the left pane (codex)
    tmux.select_pane(f'{session_name}:0.0')
    return True

def create_or_update_session(branch_dir, session_name, programs):
    """Create or update a tmux session."""
    # Create session if it doesn't exist
    if not session_exists(session_name):
        if not setup_tmux_session(branch_dir, session_name):
            return

        # After layout is set up, launch programs
        launch_programs(session_name, programs)
    else:
        # Session exists but we should ensure the correct layout and commands
        if not reset_session_layout(session_name, branch_dir):
            return

        # After layout is reset, restart programs
        restart_programs(session_name, programs)

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

    # Ensure branch exists in repo config
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

    # Get programs to launch
    programs = get_programs(config, repo_url)

    # Checkout directory
    branch_dir = f"./{repo_name}.{branch_name}"
    branch_dir_path = Path(branch_dir).resolve()

    # Create directory if it doesn't exist
    if not os.path.exists(branch_dir):
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

    # Session name is just the branch name
    session_name = branch_name

    # Create or update the tmux session
    create_or_update_session(branch_dir, session_name, programs)

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