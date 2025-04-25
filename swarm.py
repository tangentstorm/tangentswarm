#!/usr/bin/env python3
import os
import sys
import yaml
import subprocess
import time
import re
import shutil
from pathlib import Path

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
    try:
        result = subprocess.run(['tmux', 'has-session', '-t', session_name],
                                stderr=subprocess.PIPE, check=False)
        return result.returncode == 0
    except Exception:
        return False

def create_or_update_session(branch_dir, session_name):
    """Create or update a tmux session."""
    # Create session if it doesn't exist
    if not session_exists(session_name):
        # Create a new session with the first window running codex
        subprocess.run([
            'tmux', 'new-session', '-d', '-s', session_name,
            '-c', branch_dir, 'exec ${SHELL:-bash} -c "codex"'
        ])

        # Create second window pane for vite
        subprocess.run([
            'tmux', 'split-window', '-h', '-t', session_name,
            '-c', branch_dir, 'exec ${SHELL:-bash} -c "APP=mn vite"'
        ])

        # Create third window pane for API
        subprocess.run([
            'tmux', 'split-window', '-v', '-t', f'{session_name}.1',
            '-c', branch_dir, 'exec ${SHELL:-bash} -c "APP=mn python api/tcode-server.py"'
        ])

        # Select the left pane (codex)
        subprocess.run(['tmux', 'select-pane', '-t', f'{session_name}.0'])
    else:
        # Session exists but we should ensure the correct layout and commands

        # First check if the session has the right number of panes
        result = subprocess.run(['tmux', 'list-panes', '-t', session_name, '-F', '#{pane_index}'],
                               stdout=subprocess.PIPE, text=True, check=True)
        panes = result.stdout.strip().split('\n')

        # Kill existing panes if layout is wrong
        if len(panes) != 3:
            # Kill all panes except the first one
            for pane in panes[1:]:
                try:
                    subprocess.run(['tmux', 'kill-pane', '-t', f'{session_name}.{pane}'], check=False)
                except Exception:
                    pass

            # Now we have only one pane, make sure it's running codex
            subprocess.run([
                'tmux', 'send-keys', '-t', f'{session_name}.0', 'C-c', 'clear && exec codex', 'Enter'
            ])

            # Add panes for vite and API
            subprocess.run([
                'tmux', 'split-window', '-h', '-t', session_name,
                '-c', branch_dir, 'exec ${SHELL:-bash} -c "APP=mn vite"'
            ])

            subprocess.run([
                'tmux', 'split-window', '-v', '-t', f'{session_name}.1',
                '-c', branch_dir, 'exec ${SHELL:-bash} -c "APP=mn python api/tcode-server.py"'
            ])
        else:
            # Send commands to ensure correct programs are running
            subprocess.run([
                'tmux', 'send-keys', '-t', f'{session_name}.0', 'C-c', 'clear && exec codex', 'Enter'
            ])
            subprocess.run([
                'tmux', 'send-keys', '-t', f'{session_name}.1', 'C-c', 'clear && APP=mn vite', 'Enter'
            ])
            subprocess.run([
                'tmux', 'send-keys', '-t', f'{session_name}.2', 'C-c', 'clear && APP=mn python api/tcode-server.py', 'Enter'
            ])

        # Select the left pane (codex)
        subprocess.run(['tmux', 'select-pane', '-t', f'{session_name}.0'])

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
        subprocess.run(['git', 'checkout', branch_name], cwd=branch_dir, check=False)
        subprocess.run(['git', 'pull'], cwd=branch_dir, check=False)

    # Session name is just the branch name
    session_name = branch_name

    # Create or update the tmux session
    create_or_update_session(branch_dir, session_name)

    # Replace current process with tmux attach
    os.execvp('tmux', ['tmux', '-u', 'attach-session', '-t', session_name])

if __name__ == "__main__":
    main()
