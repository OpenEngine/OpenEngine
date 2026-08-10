"""Composition root for the worker.

Deliberately a sibling of the control server's composition rather than shared
code: the two processes will diverge (the worker needs an agent runner and a
workspace provider on real hardware; the control server mostly needs ingress and
state). Sharing them now would couple two deployables that should be free to
move independently.
"""

from dataclasses import dataclass

from engine.adapters.communications import BuzzCommunications
from engine.adapters.github import GitHubSourceControl
from engine.adapters.postgres import PostgresStateStore
from engine.adapters.temporal import TemporalWorkflowRuntime
from engine.adapters.workspace import GitWorktreeWorkspaceProvider
from engine.runtime import Capabilities, Dispatcher, resolve_agent_runner


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
    #: Agent backends to try, in order. Resolved from installed plugins, so the
    #: worker names no agent vendor either -- same rule as the control server.
    runner_preference: tuple[str, ...] = ("anthropic", "scripted")


def choose_agent_runner(settings: Settings) -> object:
    """First installed, usable backend from the preference list."""
    runner, _ = resolve_agent_runner(settings.runner_preference)
    return runner


def build_capabilities(settings: Settings) -> Capabilities:
    """Wire every port to its concrete implementation.

    The five infrastructure capabilities are imported directly: the worker is a
    deployable we operate, so their composition root is genuinely ours. The agent
    runner is not -- which backend runs an agent is the operator's call, so it
    resolves by name.
    """
    return Capabilities(
        workflow_runtime=TemporalWorkflowRuntime(settings.temporal_host, task_queue=settings.task_queue),
        source_control=GitHubSourceControl(settings.github_token),
        agent_runner=choose_agent_runner(settings),
        communications=BuzzCommunications(settings.buzz_base_url, settings.buzz_api_token),
        workspace_provider=GitWorktreeWorkspaceProvider(settings.workspace_root),
        state_store=PostgresStateStore(settings.postgres_dsn),
    )


def build_dispatcher(settings: Settings) -> Dispatcher:
    """The worker's job: turn engine commands into real effects."""
    return Dispatcher(build_capabilities(settings))


__all__ = ["Settings", "build_capabilities", "build_dispatcher", "choose_agent_runner"]
