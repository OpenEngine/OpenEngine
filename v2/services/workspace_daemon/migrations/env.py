"""Alembic environment for the daemon's runtime-local journal."""

from openengine.persistence.migrations import run_environment

from workspace_daemon.store import metadata

run_environment(metadata)
