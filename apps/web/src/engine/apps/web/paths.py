"""User-owned files never live beside an installed release or depend on cwd."""

import os
from pathlib import Path

from platformdirs import user_config_path, user_data_path, user_log_path


def config_directory() -> Path:
    return Path(os.environ.get("ENGINE_CONFIG_DIR", user_config_path("openengine"))).expanduser()


def data_directory() -> Path:
    path = Path(os.environ.get("ENGINE_DATA_DIR", user_data_path("openengine"))).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def log_directory() -> Path:
    path = Path(os.environ.get("ENGINE_LOG_DIR", user_log_path("openengine"))).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()
