#!/usr/bin/env python3
"""Backwards-compatible entry point: `python swarm.py ...` still works.

The real code now lives in the `tangentswarm` package (tangentswarm/cli.py).
Prefer installing the package (`pip install .`) and running `swarm`.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

from tangentswarm.cli import main  # noqa: E402

if __name__ == "__main__":
    main()
