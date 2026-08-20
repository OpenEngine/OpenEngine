"""Where an installed OpenEngine keeps configuration and mutable state.

A source checkout can get away with writing ``conversations.sqlite3`` into
whatever directory it was launched from, because there is only ever one such
directory. An installed product cannot: the same command is run from wherever
the user happens to be standing, and "my conversations disappeared" is what
launch-directory state looks like from outside. So the location is derived from
the platform's user-data convention instead of from the current directory.

The installation directory holds no state either, which is what makes an
upgrade a matter of replacing one tree with another: nothing a user created
lives inside the thing being replaced.

Only the *defaults* are decided here. ``--config`` and ``ENGINE_CONFIG`` still
name a file outright, and ``ENGINE_DATA_DIR`` relocates the data directory
whole, so a deployment that has already chosen where its state lives keeps it.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path

DATA_DIRECTORY_ENVIRONMENT_VARIABLE = "ENGINE_DATA_DIR"

#: The directory name under an XDG base. Lowercase, because its neighbours in
#: `~/.local/share` are.
XDG_DIRECTORY_NAME = "openengine"

#: The directory name under `~/Library/Application Support`, where neighbours
#: are named the way the application is written.
MACOS_DIRECTORY_NAME = "OpenEngine"

#: The conversation database, under the data directory.
DATABASE_NAME = "conversations.sqlite3"


def user_data_directory(
    *,
    environ: Mapping[str, str] | None = None,
    platform: str | None = None,
) -> Path:
    """Where this user's OpenEngine state belongs on this platform.

    Not created here: resolving a path and making a directory are different
    decisions, and ``--version`` should not leave one behind.
    """

    environment = _environment(environ)
    if override := environment.get(DATA_DIRECTORY_ENVIRONMENT_VARIABLE):
        return Path(override).expanduser()
    if _is_macos(platform):
        return _application_support(environment)
    return _xdg_base(environment, "XDG_DATA_HOME", Path(".local") / "share")


def user_config_directory(
    *,
    environ: Mapping[str, str] | None = None,
    platform: str | None = None,
) -> Path:
    """Where this user's ``engine.toml`` belongs on this platform.

    Separate from the data directory on Linux, where the convention separates
    them, and the same directory on macOS, where it does not.
    """

    environment = _environment(environ)
    if _is_macos(platform):
        return _application_support(environment)
    return _xdg_base(environment, "XDG_CONFIG_HOME", Path(".config"))


def default_database_path(
    *,
    environ: Mapping[str, str] | None = None,
    platform: str | None = None,
) -> Path:
    """The conversation database an install uses when nobody names one."""

    return user_data_directory(environ=environ, platform=platform) / DATABASE_NAME


def ensure_parent_directory(path: str | os.PathLike[str]) -> None:
    """Make the directory a file is about to be created in.

    SQLite will make the database but not the directory holding it, and on a
    fresh install that directory does not exist yet.
    """

    Path(path).parent.mkdir(parents=True, exist_ok=True)


def _environment(environ: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if environ is None else environ


def _is_macos(platform: str | None) -> bool:
    return (sys.platform if platform is None else platform) == "darwin"


def _application_support(environment: Mapping[str, str]) -> Path:
    return _home(environment) / "Library" / "Application Support" / MACOS_DIRECTORY_NAME


def _xdg_base(
    environment: Mapping[str, str], variable: str, fallback: Path
) -> Path:
    # The XDG specification says a relative base is invalid and the default is
    # to be used instead, rather than resolved against the current directory --
    # which is the one directory this module exists to stop depending on.
    configured = environment.get(variable)
    base = Path(configured) if configured else None
    if base is None or not base.is_absolute():
        base = _home(environment) / fallback
    return base / XDG_DIRECTORY_NAME


def _home(environment: Mapping[str, str]) -> Path:
    home = environment.get("HOME")
    return Path(home) if home else Path.home()


__all__ = [
    "DATABASE_NAME",
    "DATA_DIRECTORY_ENVIRONMENT_VARIABLE",
    "MACOS_DIRECTORY_NAME",
    "XDG_DIRECTORY_NAME",
    "default_database_path",
    "ensure_parent_directory",
    "user_config_directory",
    "user_data_directory",
]
