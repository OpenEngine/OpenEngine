"""Composition root for the control server.

The one place in this app allowed to name concrete adapters. Everything below
this file is typed against `engine.ports` protocols, so swapping Temporal for a
local driver or GitHub for GitLab is an edit here and nowhere else.

Ticket 1 wires the graph with placeholder construction; configuration loading
and real credentials land with the deployment ticket.
"""

from dataclasses import dataclass
from pathlib import Path

from engine.adapters.agent_runner.codex import CodexAgentRunner
from engine.adapters.communications.buzz import BuzzCommunications
from engine.adapters.source_control.github import GitHubSourceControl
from engine.adapters.state_store.postgres import PostgresStateStore
from engine.adapters.workflow_runtime.temporal import TemporalWorkflowRuntime
from engine.adapters.workspace_provider.git_worktree import GitWorktreeWorkspaceProvider
from engine.runtime import Capabilities, EngineConfig


@dataclass(frozen=True, slots=True)
class Settings:
    """Everything the composition root needs from the environment."""

    temporal_host: str = "localhost:7233"
    github_token: str = ""
    buzz_base_url: str = ""
    buzz_api_token: str = ""
    workspace_root: str = "/tmp/engine-workspaces"
    postgres_dsn: str = ""
    engine_config: EngineConfig = EngineConfig()
    """Provider-neutral settings loaded from TOML; policy enforcement lands next."""
    config_path: Path | None = None


def build_capabilities(settings: Settings) -> Capabilities:
    """Wire every port to its concrete implementation."""
    return Capabilities(
        workflow_runtime=TemporalWorkflowRuntime(settings.temporal_host),
        source_control=GitHubSourceControl(settings.github_token),
        agent_runner=CodexAgentRunner(),
        communications=BuzzCommunications(settings.buzz_base_url, settings.buzz_api_token),
        workspace_provider=GitWorktreeWorkspaceProvider(settings.workspace_root),
        state_store=PostgresStateStore(settings.postgres_dsn),
    )


__all__ = ["Settings", "build_capabilities"]
