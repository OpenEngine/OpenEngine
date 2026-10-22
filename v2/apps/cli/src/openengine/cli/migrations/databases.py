"""Bring each service's database to the head of its migration history.

`OE_DATABASE_BACKEND` picks the backend for all of them; see
packages/persistence. Upgrading never drops data a revision does not drop.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from typing import Any

from alembic.util import CommandError
from connection_bus.persistence import PERSISTENCE as CONNECTION_BUS
from graph_engine.persistence import PERSISTENCE as GRAPH_ENGINE
from sqlalchemy.exc import SQLAlchemyError
from tool_service.persistence import PERSISTENCE as TOOL_SERVICE
from virtual_control_plane.persistence import PERSISTENCE as VIRTUAL_CONTROL_PLANE

from openengine.cli.migrations import MigrationError
from openengine.persistence import BackendNotImplemented, ConfigError, DatabaseConfig, ServicePersistence
from openengine.persistence.migrations import Upgrade

SERVICES: Sequence[ServicePersistence[Any]] = (CONNECTION_BUS, GRAPH_ENGINE, TOOL_SERVICE, VIRTUAL_CONTROL_PLANE)


class Outcome(StrEnum):
    CREATED = "created"
    UPGRADED = "upgraded"
    UNCHANGED = "unchanged"


def outcome(upgrade: Upgrade) -> Outcome:
    if upgrade.before == upgrade.after:
        return Outcome.UNCHANGED
    return Outcome.CREATED if upgrade.before is None else Outcome.UPGRADED


class InvalidDatabaseConfig(MigrationError):
    def __init__(self, cause: ConfigError) -> None:
        super().__init__(str(cause))


class DatabaseMigrationFailed(MigrationError):
    def __init__(self, service: str, cause: Exception) -> None:
        super().__init__(f"could not migrate the {service} database: {cause}")


def database_config() -> DatabaseConfig:
    try:
        return DatabaseConfig.from_environment()
    except ConfigError as error:
        raise InvalidDatabaseConfig(error) from error


def migrate_databases(config: DatabaseConfig, services: Sequence[ServicePersistence[Any]] = SERVICES) -> list[Upgrade]:
    upgrades = []
    for service in services:
        try:
            upgrades.append(service.upgrade(config))
        except (BackendNotImplemented, CommandError, SQLAlchemyError) as error:
            raise DatabaseMigrationFailed(service.name, error) from error
    return upgrades
