"""Resolve the three latest stable CLI releases for one compatibility run."""

import json
import re
import subprocess

PACKAGES = {
    "codex": "@openai/codex",
    "claude": "@anthropic-ai/claude-code",
}


def latest_versions(package: str) -> list[str]:
    published = json.loads(subprocess.check_output(
        ["npm", "view", package, "versions", "--json"], text=True, timeout=60
    ))
    if not isinstance(published, list):
        raise ValueError(f"{package}: expected a list of published versions")
    stable = {v for v in published if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", v)}
    latest = sorted(stable, key=lambda v: tuple(map(int, v.split("."))), reverse=True)[:3]
    if len(latest) != 3:
        raise ValueError(f"{package}: expected at least three stable releases")
    return latest


def main() -> None:
    # Resolve the whole matrix before emitting it; registry errors must fail
    # the run rather than silently shrink coverage or reuse stale versions.
    matrix = {"include": [
        {"provider": provider, "package": package, "version": version}
        for provider, package in PACKAGES.items()
        for version in latest_versions(package)
    ]}
    print(json.dumps(matrix, separators=(",", ":")))


if __name__ == "__main__":
    main()
