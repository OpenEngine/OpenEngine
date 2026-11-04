"""Settings for the control server."""

from dataclasses import dataclass

from engine.adapters.workspace_provider.git_worktree import DEFAULT_ROOT_DIRECTORY
from engine.runtime import ServiceSettings


@dataclass(frozen=True, slots=True)
class Settings(ServiceSettings):
    """Everything the control server needs, layered on the loaded Engine config."""

    workspace_root: str = DEFAULT_ROOT_DIRECTORY
