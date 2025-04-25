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

def load_config():
    """Load configuration from YAML file."""
    if not os.path.exists(CONFIG_FILE):
        # Default config with an example repo
        return {'example_repo': {'main': 5000}}

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

def create_or_update_session(branch_dir, session_name):
    """Create or update a tmux session."""
    # Create session if it doesn't exist
    if not session_exists(session_name):
        print(f"Creating new tmux session: {session_name}")

        # Create a new session with the shell
        result1 = tmux.new_session(session_name, branch_dir)

        # Rename the window to 'codex'
        tmux.rename_window(f'{session_name}:0', 'codex')

        # Send the codex command to the shell
        tmux.send_keys(f'{session_name}:0', 'codex')

        if result1.returncode != 0:
            print(f"Error creating session: {result1.stderr}")
            return

        # List panes to debug
        panes_result = tmux.list_panes(session_name)
        print(f"Initial panes: {panes_result.stdout}")

        # Add a small delay to ensure the session is fully initialized
        time.sleep(1)

        # Create second window pane for vite (use explicit target for first pane)
        result2 = tmux.split_window(f'{session_name}:0.0', '-h', branch_dir)

        # Send the vite command to the shell
        tmux.send_keys(f'{session_name}:0.1', 'APP=mn vite')

        if result2.returncode != 0:
            print(f"Error creating vite pane: {result2.stderr}")
            # Try to create the pane again with just the session name
            result2b = tmux.split_window(session_name, '-h', branch_dir)

            # Send the vite command to the new shell
            tmux.send_keys(f'{session_name}:0.1', 'APP=mn vite')
            if result2b.returncode != 0:
                print(f"Second attempt failed: {result2b.stderr}")
                return

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

        # Send the tcode-server command to the shell
        tmux.send_keys(f'{session_name}:0.2', 'APP=mn python api/tcode-server.py')

        # If that failed, try splitting the left pane
        if result3.returncode != 0:
            print(f"First vertical split attempt failed, trying left pane: {result3.stderr}")
            result3b = tmux.split_window(f'{session_name}:0.0', '-v', branch_dir)

            # Send the tcode-server command to the shell
            tmux.send_keys(f'{session_name}:0.2', 'APP=mn python api/tcode-server.py')

            if result3b.returncode != 0:
                print(f"Second vertical split attempt failed, trying session: {result3b.stderr}")
                # Try with just the session name
                result3c = tmux.split_window(session_name, '-v', branch_dir)

                # Send the tcode-server command to the shell
                tmux.send_keys(f'{session_name}:0.2', 'APP=mn python api/tcode-server.py')

                if result3c.returncode != 0:
                    print(f"All vertical split attempts failed: {result3c.stderr}")

        # Select the left pane (codex)
        tmux.select_pane(f'{session_name}:0.0')
    else:
        # Session exists but we should ensure the correct layout and commands

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

            # Now we have only one pane, make sure it's running codex
            tmux.send_keys(f'{session_name}.0', 'C-c', False)
            tmux.send_keys(f'{session_name}.0', 'clear')
            tmux.send_keys(f'{session_name}.0', 'codex')

            # List panes to debug
            panes_result = tmux.list_panes(session_name, '#{pane_index}')
            print(f"Initial panes (update): {panes_result.stdout}")

            # Add a small delay to ensure commands complete
            time.sleep(1)

            # Add panes for vite and API
            result2 = tmux.split_window(f'{session_name}:0.0', '-h', branch_dir)

            # Send the vite command to the shell
            tmux.send_keys(f'{session_name}:0.1', 'APP=mn vite')

            if result2.returncode != 0:
                print(f"Error creating vite pane (update): {result2.stderr}")
                # Try to create the pane again with just the session name
                result2b = tmux.split_window(session_name, '-h', branch_dir)

                # Send the vite command to the shell
                tmux.send_keys(f'{session_name}:0.1', 'APP=mn vite')
                if result2b.returncode != 0:
                    print(f"Second attempt failed (update): {result2b.stderr}")
                    return

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

            # Send the tcode-server command to the shell
            tmux.send_keys(f'{session_name}:0.2', 'APP=mn python api/tcode-server.py')

            # If that failed, try splitting the left pane
            if result3.returncode != 0:
                print(f"First vertical split attempt failed (update), trying left pane: {result3.stderr}")
                result3b = tmux.split_window(f'{session_name}:0.0', '-v', branch_dir)

                # Send the tcode-server command to the shell
                tmux.send_keys(f'{session_name}:0.2', 'APP=mn python api/tcode-server.py')

                if result3b.returncode != 0:
                    print(f"Second vertical split attempt failed (update), trying session: {result3b.stderr}")
                    # Try with just the session name
                    result3c = tmux.split_window(session_name, '-v', branch_dir)

                    # Send the tcode-server command to the shell
                    tmux.send_keys(f'{session_name}:0.2', 'APP=mn python api/tcode-server.py')

                    if result3c.returncode != 0:
                        print(f"All vertical split attempts failed (update): {result3c.stderr}")
        else:
            # Send commands to ensure correct programs are running
            tmux.send_keys(f'{session_name}:0.0', 'C-c', False)
            tmux.send_keys(f'{session_name}:0.0', 'clear')
            tmux.send_keys(f'{session_name}:0.0', 'codex')

            tmux.send_keys(f'{session_name}:0.1', 'C-c', False)
            tmux.send_keys(f'{session_name}:0.1', 'clear')
            tmux.send_keys(f'{session_name}:0.1', 'APP=mn vite')

            tmux.send_keys(f'{session_name}:0.2', 'C-c', False)
            tmux.send_keys(f'{session_name}:0.2', 'clear')
            tmux.send_keys(f'{session_name}:0.2', 'APP=mn python api/tcode-server.py')

        # Select the left pane (codex)
        tmux.select_pane(f'{session_name}:0.0')

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

    # Ensure branch exists in repo config
    if branch_name not in config[repo_url]:
        # Collect all used ports
        used_ports = set()
        for repo in config.values():
            used_ports.update(repo.values())

        # Find next available port
        port = find_next_available_port(used_ports)

        # Add new branch with port
        config[repo_url][branch_name] = port
        save_config(config)

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
    create_or_update_session(branch_dir, session_name)

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