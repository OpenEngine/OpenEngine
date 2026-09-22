"""Stable, per-user writable paths; release files are always read-only."""
from pathlib import Path

from platformdirs import user_config_path, user_data_path, user_log_path


def config_directory() -> Path:
    return user_config_path("OpenEngine", appauthor=False, ensure_exists=True)


def data_directory() -> Path:
    return user_data_path("OpenEngine", appauthor=False, ensure_exists=True)


def log_directory() -> Path:
    return user_log_path("OpenEngine", appauthor=False, ensure_exists=True)
