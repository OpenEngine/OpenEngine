"""Shared discovery policy for filesystem reads and change inspection."""

from pathlib import Path

OMIT = {".git", ".venv", "venv", "node_modules", "__pycache__", "dist", "build", ".cache", ".ov"}


def inspectable(path: str) -> bool:
    parts = Path(path).parts
    return not (
        any(part in OMIT for part in parts)
        or any(
            part.startswith(".env") and part not in {".env.example", ".env.sample"}
            for part in parts
        )
        or Path(path).suffix.lower() in {".pem", ".key", ".p12"}
    )
