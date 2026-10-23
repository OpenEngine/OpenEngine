"""Runs `openengine.contracts.validate`; see that module. From `v2/`: `uv run python packages/contracts/tooling/validate.py`."""

import sys

from openengine.contracts.validate import main

if __name__ == "__main__":
    sys.exit(main())
