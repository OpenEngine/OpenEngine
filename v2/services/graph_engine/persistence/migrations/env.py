"""Alembic environment for the graph engine's database, on whichever backend is configured."""

from openengine.persistence.migrations import run_environment

from graph_engine.persistence.schema import metadata

run_environment(metadata)
