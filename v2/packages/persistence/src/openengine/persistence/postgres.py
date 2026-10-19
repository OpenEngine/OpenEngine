"""Postgres: a stub until a deployment needs it.

Services share one database and each keeps its tables in a schema named after
it. Table definitions stay schema-free so they run on SQLite too; connections
put the service's schema first on the search_path instead.

Selecting it fails on first use rather than at startup, so a service can be
configured for Postgres and still report what it is missing.
"""

from __future__ import annotations

from sqlalchemy.engine import URL

from openengine.persistence.config import Backend
from openengine.persistence.database import BackendNotImplemented, Database


class PostgresDatabase(Database):
    backend = Backend.POSTGRES

    def __init__(self, service: str, url: str | None) -> None:
        super().__init__(service)
        self.postgres_url = url
        self.schema = service

    def url(self, *, asynchronous: bool = True) -> URL:
        raise BackendNotImplemented(self.backend, "connections")
