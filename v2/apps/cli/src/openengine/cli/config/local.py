"""Where `oe` keeps local state, and the defaults it starts the supervisor with."""

from __future__ import annotations

import os
from pathlib import Path

HOST = "127.0.0.1"
NATS_PORT = 4222
NATS_MONITOR_PORT = 8222


def _xdg(variable: str, default: str) -> Path:
    value = os.environ.get(variable)
    return Path(value) if value and Path(value).is_absolute() else Path.home() / default


def cache_directory() -> Path:
    return _xdg("XDG_CACHE_HOME", ".cache") / "openengine"


def state_directory() -> Path:
    return _xdg("XDG_STATE_HOME", ".local/state") / "openengine"


def supervisor_log() -> Path:
    return state_directory() / "logs" / "supervisor.log"
