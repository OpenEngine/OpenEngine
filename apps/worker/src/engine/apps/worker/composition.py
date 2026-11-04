"""Composition root for the worker.

Deliberately a sibling of the control server's composition rather than shared
code: the two processes will diverge (the worker needs an agent runner and a
workspace provider on real hardware; the control server mostly needs ingress and
state). Sharing them now would couple two deployables that should be free to
move independently.
"""

import logging

from engine.adapters.agent_runner.acp import codex_acp_runner
from engine.adapters.communications.buzz import BuzzCommunications
from engine.adapters.sandbox.process import ProcessSandbox
from engine.adapters.sandbox.smolvm import SmolvmSandbox
from engine.adapters.source_control.github import GitHubSourceControl
from engine.adapters.state_store.postgres import PostgresStateStore
from engine.adapters.workflow_runtime.temporal import TemporalWorkflowRuntime
from engine.adapters.workspace_provider.git_worktree import (
    DEFAULT_ROOT_DIRECTORY,
    GitWorktreeWorkspaceProvider,
)
from engine.runtime import Capabilities, Dispatcher
from engine.apps.worker.settings import Settings


def build_capabilities(settings: Settings) -> Capabilities:
    """Wire every port to its concrete implementation."""
    workspace_provider = GitWorktreeWorkspaceProvider(settings.workspace_root or DEFAULT_ROOT_DIRECTORY)
    logging.getLogger(__name__).log(
        logging.INFO if settings.github_token else logging.WARNING,
        "source_control composition=worker github_identity=service credential=settings.github_token configured=%s",
        bool(settings.github_token),
    )
    sandbox = (
        SmolvmSandbox(image=settings.engine_config.sandbox.image or None)
        if settings.engine_config.sandbox.backend == "smolvm" else ProcessSandbox()
    )
    return Capabilities(
        sandbox=sandbox,
        workflow_runtime=TemporalWorkflowRuntime(settings.temporal_host, task_queue=settings.task_queue),
        source_control=GitHubSourceControl(
            settings.github_token, workspace_provider=workspace_provider,
            resolve_addressed_threads=settings.engine_config.github.resolve_addressed_threads,
            host_aliases=settings.engine_config.github.host_aliases
        ),
        agent_runner=codex_acp_runner(
            sandbox="read-only",
            attribution=settings.engine_config.attribution,
            config=settings.engine_config,
        ),
        communications=BuzzCommunications(settings.buzz_base_url, settings.buzz_api_token),
        workspace_provider=workspace_provider,
        state_store=PostgresStateStore(settings.postgres_dsn),
    )


def build_dispatcher(settings: Settings) -> Dispatcher:
    """The worker's job: turn engine commands into real effects."""
    return Dispatcher(build_capabilities(settings))


__all__ = ["Settings", "build_capabilities", "build_dispatcher"]
