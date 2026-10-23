"""Runs `openengine.contracts.resources`; see that module. From `v2/`:

    uv run python packages/contracts/tooling/resources.py               # regenerate the references
    uv run python packages/contracts/tooling/resources.py check FILE..  # validate resource files
"""

import sys

from openengine.contracts.resources import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
