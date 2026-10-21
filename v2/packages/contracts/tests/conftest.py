import sys
from pathlib import Path

# The validator is a script under tooling/, not an installed package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tooling"))
