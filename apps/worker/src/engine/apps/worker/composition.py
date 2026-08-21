"""Composition root for the worker.

Deliberately a sibling of the control server's composition rather than shared
code: the two processes will diverge (the worker needs an agent runner and a
workspace provider on real hardware; the control server mostly needs ingress and
state). Sharing them now would couple two deployables that should be free to
move independently.
"""

from dataclasses import dataclass
from pathlib import Path

from engine.adapters.agent_runner.codex import CodexAgentRunner
from engine.adapters.communications.buzz import BuzzCommunications
from engine.adapters.source_control.github import GitHubSourceControl
from engine.adapters.state_store.postgres import PostgresStateStore
from engine.adapters.workflow_runtime.temporal import TemporalWorkflowRuntime
from engine.adapters.workspace_provider.git_worktree import GitWorktreeWorkspaceProvider
from engine.runtime import Capabilities, Dispatcher, EngineConfig


@dataclass(frozen=True, slots=True)
class Settings:
    """Everything the worker needs from the environment."""

    temporal_host: str = "localhost:7233"
    task_queue: str = "engine"
    github_token: str = ""
    buzz_base_url: str = ""
    buzz_api_token: str = ""
    workspace_root: str = "/tmp/engine-workspaces"
    postgres_dsn: str = ""
    engine_config: EngineConfig = EngineConfig()
    """Provider-neutral settings loaded from TOML.

    `approvals` governs turns that can pause to ask, and this process runs none:
    its runner is the unattended one, held to its sandbox rather than to a
    policy. Read here so the worker refuses to start on a file the interface
    would refuse too.
    """
    config_path: Path | None = None


def build_capabilities(settings: Settings) -> Capabilities:
    """Wire every port to its concrete implementation."""
    return Capabilities(
        workflow_runtime=TemporalWorkflowRuntime(settings.temporal_host, task_queue=settings.task_queue),
        source_control=GitHubSourceControl(settings.github_token),
        agent_runner=CodexAgentRunner(attribution=settings.engine_config.attribution),
        communications=BuzzCommunications(settings.buzz_base_url, settings.buzz_api_token),
        workspace_provider=GitWorktreeWorkspaceProvider(settings.workspace_root),
        state_store=PostgresStateStore(settings.postgres_dsn),
    )


def build_dispatcher(settings: Settings) -> Dispatcher:
    """The worker's job: turn engine commands into real effects."""
    return Dispatcher(build_capabilities(settings))


__all__ = ["Settings", "build_capabilities", "build_dispatcher"]
