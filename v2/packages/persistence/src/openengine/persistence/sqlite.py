"""SQLite: one file per service, so services never contend for a write lock."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from time import monotonic
from typing import Any

from sqlalchemy import Engine, event
from sqlalchemy.engine import URL

from openengine.persistence.config import Backend
from openengine.persistence.database import Database

BUSY_TIMEOUT_MILLISECONDS = 5000

# WAL lets readers run alongside the one writer; foreign keys are off in SQLite
# unless each connection turns them on.
PRAGMAS = (
    f"PRAGMA busy_timeout={BUSY_TIMEOUT_MILLISECONDS}",
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA foreign_keys=ON",
)


async def _configure_connection(connection: Any) -> None:
    cursor = await connection.cursor()
    try:
        deadline = monotonic() + BUSY_TIMEOUT_MILLISECONDS / 1000
        for pragma in PRAGMAS:
            while True:
                try:
                    await cursor.execute(pragma)
                    break
                except sqlite3.OperationalError as error:
                    # Concurrent first connections can both try to enable WAL.
                    # SQLite can return BUSY immediately for that transition,
                    # without invoking its busy handler. Retry setup, before any
                    # application transaction, within the same timeout budget.
                    if error.sqlite_errorcode != sqlite3.SQLITE_BUSY or monotonic() >= deadline:
                        raise
                    await asyncio.sleep(0.01)
                    remaining = max(1, int((deadline - monotonic()) * 1000))
                    await cursor.execute(f"PRAGMA busy_timeout={remaining}")
        await cursor.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MILLISECONDS}")
    finally:
        await cursor.close()


def _apply_pragmas(connection: Any, _record: Any) -> None:
    # run_async yields to the event loop while aiosqlite configures its raw
    # connection. A synchronous sleep here would block other writers entirely.
    connection.run_async(_configure_connection)


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
