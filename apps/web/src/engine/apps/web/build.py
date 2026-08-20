"""What this build is, for anyone who has to answer "which one am I running?".

Two questions get asked of an installed product that never get asked of a
checkout: which version is this, and which commit was it built from. A checkout
answers the second one with ``git``; an archive on someone else's machine has
no repository to ask, so the answer is written into the build.

``build.json`` is placed beside this module by the packaging script, into the
installed tree rather than into the source tree -- a checkout has nothing to
declare and should not carry a stale file saying otherwise. Without it the
version comes from package metadata and the commit is unknown, which is exactly
what a development install is.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version as installed_version
from pathlib import Path

#: The distribution whose version is OpenEngine's version.
DISTRIBUTION = "engine-web"

#: Written by `packaging/build-archive.sh` into the packaged application.
BUILD_FILE = Path(__file__).resolve().parent / "build.json"

UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class BuildInfo:
    """The identity of one built OpenEngine."""

    version: str
    commit: str

    def __str__(self) -> str:
        return f"openengine {self.version} ({self.commit})"


def build_info() -> BuildInfo:
    """Read the packaged build identity, falling back to what is installed."""

    recorded = _recorded()
    return BuildInfo(
        version=recorded.get("version") or _installed_version(),
        commit=recorded.get("commit") or UNKNOWN,
    )


def _recorded() -> dict[str, str]:
    try:
        document = json.loads(BUILD_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # A build file that is missing or unreadable means "not a release
        # build", which is a fact about this install rather than an error to
        # stop `--version` from answering.
        return {}
    if not isinstance(document, dict):
        return {}
    return {
        key: value
        for key, value in document.items()
        if isinstance(key, str) and isinstance(value, str)
    }


def _installed_version() -> str:
    try:
        return installed_version(DISTRIBUTION)
    except PackageNotFoundError:
        return UNKNOWN


__all__ = ["BUILD_FILE", "DISTRIBUTION", "UNKNOWN", "BuildInfo", "build_info"]
