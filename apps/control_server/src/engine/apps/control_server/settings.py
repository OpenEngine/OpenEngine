"""Settings for the control server, the only module here that reads the environment."""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from engine.adapters.workspace_provider.git_worktree import DEFAULT_ROOT_DIRECTORY
from engine.runtime import EngineConfig, LoadedEngineConfig


@dataclass(frozen=True, slots=True)
class Settings:
    """Everything the composition root needs, layered on the loaded Engine config."""

    temporal_host: str = "localhost:7233"
    github_token: str = ""
    buzz_base_url: str = ""
    buzz_api_token: str = ""
    workspace_root: str = DEFAULT_ROOT_DIRECTORY
    postgres_dsn: str = ""
    engine_config: EngineConfig = EngineConfig()
    """Provider-neutral settings loaded from TOML.

    `approvals` governs turns that can pause to ask, and this process runs none:
    its runner is the unattended one, held to its sandbox rather than to a
    policy. Read here so the server refuses to start on a file the interface
    would refuse too.
    """
    config_path: Path | None = None

    @classmethod
    def from_env(
        cls, loaded: LoadedEngineConfig, environ: Mapping[str, str] | None = None
    ) -> "Settings":
        """Build once at startup: the process environment overrides the TOML config."""
        environment = os.environ if environ is None else environ
        return cls(
            engine_config=loaded.config,
            config_path=loaded.path,
            github_token=environment.get("GITHUB_TOKEN", loaded.config.github_token),
        )
