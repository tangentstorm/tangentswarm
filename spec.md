swarm.py is a python program that does the following:

* maintains a list of checked out branches in a git repo in the file
  `swarm.yaml`, with the following characteristics:
 
  - the top level is a list of repo names
  - under each repo, it maps a branch name to a port number

* has the argument: [<repo name>] <branch name>, where the repo defaults to the first repo.

* checks out the repo to the directory `./REPO_NAME.BRANCH_NAME` (skipping if the directory already exists)
  note that the repo name is just the last part of the repo url stored in the yaml file.

* spawns a new tmux session (if it doesn't exist) named BRANCH_NAME rooted in the branch directory.

* ensures that three tmux windows are running in that session, each wrapped in the user's shell and running from the branch directory:
  - pane on the left running the program codex (inside a shell)
  - pane on upper right running the program APP=mn vite (inside a shell)
  - pane on lower right running APP=mn python api/tcode-server.py
  - replaces itself with tmux -u -t <session name>

Note that the branch might already be checked out and the various
programs might already be running, but might not be arranged correctly.

