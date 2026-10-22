"""Alembic environment for the tool service's database, on whichever backend is configured."""

from openengine.persistence.migrations import run_environment

from tool_service.persistence.schema import metadata

run_environment(metadata)
