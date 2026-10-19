"""The Rock AI rebrand leaves no trace of the old product name in the tree.

The old name is assembled from pieces so this file is not itself a match for
the repository-wide search it guards.
"""

import subprocess

import yaml

from layout import REPO_ROOT

OLD_NAME = "PA" + "TY"
TESTS_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "tests.yml"


def test_no_tracked_file_names_the_old_brand():
    found = subprocess.run(
        ["git", "grep", "-niI", OLD_NAME],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    # `git grep` searches tracked files, skips binaries (-I), and is
    # case-insensitive (-i); it exits 0 only when a match is found.
    assert found.returncode != 0, found.stdout


def smoke_mkdir_lines():
    workflow = yaml.safe_load(TESTS_WORKFLOW.read_text())
    lines = []
    for job in workflow["jobs"].values():
        for step in job.get("steps", []):
            for line in step.get("run", "").splitlines():
                if line.strip().startswith("mkdir -p") and "$HOME/code/" in line:
                    lines.append(line.strip())
    return lines


def test_startup_smoke_steps_create_the_rock_checkout():
    lines = smoke_mkdir_lines()
    expected = 'mkdir -p "$HOME/code/OpenEngine" "$HOME/code/Rock"'
    # The "Run OpenEngine" step and the control-server/worker/web --check step.
    assert lines == [expected, expected]
