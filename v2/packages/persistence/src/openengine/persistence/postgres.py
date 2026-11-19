"""Postgres 16+ connections to pre-created service schemas; no runtime DDL."""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import URL
from sqlalchemy.pool import NullPool

from openengine.persistence.config import (
    POSTGRES_MIGRATION_URL_VARIABLE,
    POSTGRES_URL_VARIABLE,
    Backend,
    ConfigError,
    postgres_url,
)
from openengine.persistence.database import Database


class InvalidSchema(ConfigError):
    def __init__(self) -> None:
        super().__init__("Postgres service schema must be a non-reserved lower-case identifier (1-63 characters)")


class UnsupportedVersion(ConfigError):
    def __init__(self) -> None:
        super().__init__("Postgres 16 or newer is required")


class MissingSchema(ConfigError):
    def __init__(self, schema: str) -> None:
        super().__init__(f"Postgres schema {schema!r} must exist and allow this connection USAGE")


class PostgresDatabase(Database):
    backend = Backend.POSTGRES

    def __init__(self, service: str, url: str | None, migration_url: str | None = None) -> None:
        super().__init__(service)
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", service) or service == "public" or service.startswith("pg_"):
            raise InvalidSchema()
        self._runtime_url = url
        self._migration_url = migration_url
        self.schema = service

    def url(self, *, asynchronous: bool = True) -> URL:
        value, variable = ((self._runtime_url, POSTGRES_URL_VARIABLE) if asynchronous else
                           (self._migration_url, POSTGRES_MIGRATION_URL_VARIABLE))
        url = postgres_url(value, variable)
        return url.set(drivername="postgresql+psycopg_async" if asynchronous else "postgresql+psycopg").update_query_dict(
            {"options": f'-csearch_path="{self.schema}"'})

    def configure(self, engine: Engine) -> None:
        @event.listens_for(engine, "connect")
        def check(connection: Any, _record: Any) -> None:
            with connection.cursor() as cursor:
                cursor.execute("SELECT current_setting('server_version_num')::integer, current_schema()")
                version, schema = cursor.fetchone()
            connection.rollback()
            if version < 160000:
                raise UnsupportedVersion()
            if schema != self.schema:
                raise MissingSchema(self.schema)

    def migration_engine(self) -> Engine:
        engine = create_engine(self.url(asynchronous=False), poolclass=NullPool)
        self.configure(engine)
        return engine
