"""Alembic environment for the connection bus's database, on whichever backend is configured."""

from openengine.persistence.migrations import run_environment

from connection_bus.persistence.schema import metadata

run_environment(metadata)
