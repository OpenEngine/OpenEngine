"""Test-wide fixtures.

The one thing here is environment isolation: this suite exercises code whose
configuration is selected from the ambient environment, so what the developer
running it happens to have exported would otherwise decide what is under test.
"""

import pytest

from engine.runtime.config import CONFIG_ENVIRONMENT_VARIABLE


@pytest.fixture(autouse=True)
def _engine_config_is_not_inherited(monkeypatch):
    """Keep an exported ``ENGINE_CONFIG`` out of every test.

    A developer with Engine running has this set to their own checkout's
    `engine.toml`, which names workflow and worktree directories outside this
    one; tests that load configuration without naming a file would then read
    that deployment's rather than the repository's. Tests wanting a particular
    configuration still set the variable themselves -- this only removes what
    nobody asked for.
    """

    monkeypatch.delenv(CONFIG_ENVIRONMENT_VARIABLE, raising=False)
