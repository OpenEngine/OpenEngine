"""The connection bus's store on Postgres: a stub until the backend is."""

from __future__ import annotations

from openengine.persistence import Backend, BackendNotImplemented, TenantDatabase


class PostgresConnections:
    def __init__(self, database: TenantDatabase) -> None:
        raise BackendNotImplemented(Backend.POSTGRES, "the connection bus's store")
