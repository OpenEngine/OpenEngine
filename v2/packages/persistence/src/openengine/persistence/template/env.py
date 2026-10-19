"""Alembic environment for this service's database, on whichever backend is configured."""

from openengine.persistence.migrations import run_environment

run_environment()
