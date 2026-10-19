"""The Rock AI rebrand leaves no trace of the old product name in the tree.

The old name is assembled from pieces so this file is not itself a match for
the repository-wide search it guards.
"""

import subprocess

import yaml

from layout import REPO_ROOT

OLD_NAME = "PA" + "TY"
TESTS_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "tests.yml"


def tracked_files():
    listed = subprocess.run(
        ["git", "ls-files", "-z"], cwd=REPO_ROOT, check=True, capture_output=True,
    ).stdout.decode().split("\0")
    return [REPO_ROOT / name for name in listed if name and "node_modules/" not in name]


def test_no_tracked_file_names_the_old_brand():
    needle = OLD_NAME.casefold()
    hits = []
    for path in tracked_files():
        if not path.is_file():
            continue
        data = path.read_bytes()
        if b"\0" in data:
            continue  # binary, as `grep -I` skips it
        for number, line in enumerate(data.decode(errors="replace").splitlines(), 1):
            if needle in line.casefold():
                hits.append(f"{path.relative_to(REPO_ROOT)}:{number}: {line.strip()}")
    assert hits == []


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
