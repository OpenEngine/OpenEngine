"""Alembic environment for the virtual control plane's database, on whichever backend is configured."""

from openengine.persistence.migrations import run_environment

from virtual_control_plane.persistence.schema import metadata

run_environment(metadata)
