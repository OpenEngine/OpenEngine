"""Run policy regression tests, then the full baseline comparison."""

from pathlib import Path
import subprocess
import sys


if __name__ == "__main__":
    directory = Path(__file__).resolve().parent
    tests = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", str(directory)],
                           check=False)
    if tests.returncode:
        sys.exit(tests.returncode)
    sys.exit(subprocess.run([sys.executable, str(directory / "duplicates.py"), *sys.argv[1:]],
                            check=False).returncode)
