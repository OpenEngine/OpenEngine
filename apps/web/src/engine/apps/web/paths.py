"""Stable user locations shared by installed and development entrypoints."""
from pathlib import Path

from platformdirs import user_config_path, user_data_path, user_log_path


def config_directory() -> Path:
    return user_config_path("openengine")


def data_directory() -> Path:
    path = user_data_path("openengine")
    path.mkdir(parents=True, exist_ok=True)
    return path


def log_directory() -> Path:
    path = user_log_path("openengine")
    path.mkdir(parents=True, exist_ok=True)
    return path
