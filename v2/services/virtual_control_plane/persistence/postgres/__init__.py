"""The virtual control plane's store on Postgres: a stub until the backend is."""

from __future__ import annotations

from openengine.persistence import Backend, BackendNotImplemented, Database


class PostgresWorkspaces:
    def __init__(self, database: Database) -> None:
        raise BackendNotImplemented(Backend.POSTGRES, "the virtual control plane's store")
