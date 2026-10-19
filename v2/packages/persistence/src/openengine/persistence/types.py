"""Column types that behave the same on every backend."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, Dialect
from sqlalchemy.types import TypeDecorator


class NaiveDateTime(ValueError):
    def __init__(self, value: datetime) -> None:
        super().__init__(f"{value!r} has no timezone")


class UtcDateTime(TypeDecorator[datetime]):
    """A timezone-aware UTC timestamp. SQLite drops the zone, so it is put back on read."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise NaiveDateTime(value)
        return value.astimezone(UTC)

    def process_result_value(self, value: Any, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
