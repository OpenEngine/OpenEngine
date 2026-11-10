"""Fixtures shared by the VCP tests that run a real nats-server."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from openengine.supervisor.processes.nats import install_nats_server


@pytest.fixture(scope="session")
def binary() -> Path:
    """The pinned nats-server, found or downloaded once into a shared temporary cache."""
    return install_nats_server(Path(tempfile.gettempdir()) / "oe-test-nats")
