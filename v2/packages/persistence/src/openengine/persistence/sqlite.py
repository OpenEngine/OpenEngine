"""SQLite: one file per service, so services never contend for a write lock."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import Engine, event
from sqlalchemy.engine import URL

from openengine.persistence.config import Backend
from openengine.persistence.database import Database

BUSY_TIMEOUT_MILLISECONDS = 5000

# WAL lets readers run alongside the one writer; foreign keys are off in SQLite
# unless each connection turns them on.
PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA foreign_keys=ON",
    f"PRAGMA busy_timeout={BUSY_TIMEOUT_MILLISECONDS}",
)


def _apply_pragmas(connection: Any, _record: Any) -> None:
    cursor = connection.cursor()
    try:
        for pragma in PRAGMAS:
            cursor.execute(pragma)
    finally:
        cursor.close()


class SqliteDatabase(Database):
    backend = Backend.SQLITE

    def __init__(self, service: str, directory: Path) -> None:
        super().__init__(service)
        self.path = directory / f"{service}.sqlite3"

    def url(self, *, asynchronous: bool = True) -> URL:
        return URL.create("sqlite+aiosqlite" if asynchronous else "sqlite", database=str(self.path))

    def prepare(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def configure(self, engine: Engine) -> None:
        event.listen(engine, "connect", _apply_pragmas)
