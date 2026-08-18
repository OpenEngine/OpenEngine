"""Composition root for the web control interface.

The one file in this app allowed to name concrete adapters, and a sibling of the
other two compositions rather than shared code with them -- for the same reason
the worker's is: these processes will diverge, and sharing now would couple
three deployables that should be free to move independently.

Three of the six capabilities here are real. `agent_runner` shells out to a
coding CLI, `state_store` persists conversations in SQLite, and
`workspace_provider` gives every chat an isolated Git worktree. The other three
remain wired for the composition report but are not exposed by the chat API.

`Capabilities` holds one runner because a port has one implementation, and that
is the one anything non-interactive uses. The interface additionally offers a
*choice* of runner, which is `build_runners` -- a name-to-implementation mapping
of exactly the kind a composition root exists to own.

The state store is SQLite rather than Postgres: conversations survive a process
restart without requiring an external database service.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from engine.adapters.agent_runner.claude_code import (
    READ_ONLY_TOOLS,
    WORKSPACE_WRITE_TOOLS,
    ClaudeCodeAgentRunner,
)
from engine.adapters.agent_runner.codex import CodexAgentRunner
from engine.adapters.communications.buzz import BuzzCommunications
from engine.adapters.source_control.github import GitHubSourceControl
from engine.adapters.state_store.sqlite import SQLiteStateStore
from engine.adapters.workflow_runtime.temporal import TemporalWorkflowRuntime
from engine.adapters.workspace_provider.git_worktree import GitWorktreeWorkspaceProvider
from engine.ports import AgentRunner
from engine.runtime import AgentSession, Capabilities, EngineConfig


@dataclass(frozen=True, slots=True)
class Settings:
    """Everything the interface needs from the environment.

    `host` and `port` are handed to Uvicorn by `__main__`; the rest are adapter
    arguments. Loading them from the environment lands with the deployment
    ticket, along with the other two roots.

    Frozen so one immutable settings value can be shared by the server wiring.
    """

    host: str = "localhost"
    port: int = 8000
    codex_binary: str = "codex"
    codex_sandbox: str = "read-only"
    """What a turn nobody is watching may do: read, and nothing else.

    `build_capabilities` wires the one runner a non-interactive caller reaches
    for, so it gets the sandbox that needs no one present. Chat is the other
    case and takes `interactive_codex_sandbox`.
    """
    codex_working_directory: str = "."
    codex_timeout_seconds: float | None = None
    """No ceiling: a turn runs until it is done or someone cancels it."""
    codex_model: str = ""
    claude_binary: str = "claude"
    claude_working_directory: str = "."
    claude_timeout_seconds: float | None = None
    """Same as `codex_timeout_seconds`."""
    claude_model: str = ""
    interactive_codex_sandbox: str = "workspace-write"
    """An approved command has to be able to do the thing it was approved for.

    The read-only sandbox would refuse the write after the user allowed it, and
    on-request approval is what keeps that from meaning "unattended": Codex
    stops and asks before stepping outside the worktree.
    """
    interactive_claude_allowed_tools: tuple[str, ...] = READ_ONLY_TOOLS
    """Reads are preapproved; Bash and edits reach the user as requests.

    Claude Code has no OS-level sandbox to fall back on, so the allow-list is
    the whole of the distinction: what is on it runs unattended, and everything
    else -- shell commands included -- goes to the permission callback.
    """
    workflow_codex_sandbox: str = "workspace-write"
    """Workflow implementation may edit only its isolated worktree."""
    workflow_claude_allowed_tools: tuple[str, ...] = WORKSPACE_WRITE_TOOLS
    """Claude workflows may read and edit files, but not run unrestricted Bash."""
    temporal_host: str = "localhost:7233"
    github_token: str = ""
    buzz_base_url: str = ""
    buzz_api_token: str = ""
    workspace_root: str = "/tmp/engine-workspaces"
    sqlite_path: str = "conversations.sqlite3"
    engine_config: EngineConfig = EngineConfig()
    """Provider-neutral settings loaded from TOML; runner translation lands next."""
    config_path: Path | None = None
    """The single TOML source, or ``None`` when built-in defaults are active."""


def build_capabilities(settings: Settings) -> Capabilities:
    """Wire every port to its concrete implementation."""
    workspace_provider = GitWorktreeWorkspaceProvider(settings.workspace_root)
    return Capabilities(
        workflow_runtime=TemporalWorkflowRuntime(settings.temporal_host),
        source_control=GitHubSourceControl(settings.github_token),
        agent_runner=CodexAgentRunner(
            binary_path=settings.codex_binary,
            timeout_seconds=settings.codex_timeout_seconds,
            sandbox=settings.codex_sandbox,
            working_directory=settings.codex_working_directory,
            model=settings.codex_model,
            workspace_provider=workspace_provider,
        ),
        communications=BuzzCommunications(settings.buzz_base_url, settings.buzz_api_token),
        workspace_provider=workspace_provider,
        state_store=SQLiteStateStore(settings.sqlite_path),
    )


def build_runners(settings: Settings) -> Mapping[str, AgentRunner]:
    """Every agent runner this process offers, by the name the interface shows.

    The one place a runner name is bound to an implementation -- below this file
    "codex" and "claude" are opaque strings, exactly like tool grants. The first
    entry is the default, so it is also what a conversation gets when nobody
    picks.

    One entry per CLI: the dropdown names the agent you are talking to, not the
    transport it is driven over. Both pause for approval, because a runner that
    could only run unattended is not a second choice worth offering -- what it
    would do without asking, these do after asking.
    """
    workspace_provider = GitWorktreeWorkspaceProvider(settings.workspace_root)
    return {
        "codex": CodexAgentRunner(
            binary_path=settings.codex_binary,
            timeout_seconds=settings.codex_timeout_seconds,
            sandbox=settings.interactive_codex_sandbox,
            working_directory=settings.codex_working_directory,
            model=settings.codex_model,
            workspace_provider=workspace_provider,
        ),
        "claude": ClaudeCodeAgentRunner(
            binary_path=settings.claude_binary,
            timeout_seconds=settings.claude_timeout_seconds,
            allowed_tools=settings.interactive_claude_allowed_tools,
            working_directory=settings.claude_working_directory,
            model=settings.claude_model,
            workspace_provider=workspace_provider,
        ),
    }


def build_workflow_runners(settings: Settings) -> Mapping[str, AgentRunner]:
    """Write-enabled runners used only for workflow implementation steps."""
    workspace_provider = GitWorktreeWorkspaceProvider(settings.workspace_root)
    return {
        "codex": CodexAgentRunner(
            binary_path=settings.codex_binary,
            timeout_seconds=settings.codex_timeout_seconds,
            sandbox=settings.workflow_codex_sandbox,
            working_directory=settings.codex_working_directory,
            model=settings.codex_model,
            workspace_provider=workspace_provider,
        ),
        "claude": ClaudeCodeAgentRunner(
            binary_path=settings.claude_binary,
            timeout_seconds=settings.claude_timeout_seconds,
            allowed_tools=settings.workflow_claude_allowed_tools,
            working_directory=settings.claude_working_directory,
            model=settings.claude_model,
            workspace_provider=workspace_provider,
        ),
    }


def build_session(
    capabilities: Capabilities,
    runners: Mapping[str, AgentRunner],
    repository: str = ".",
) -> AgentSession:
    """Conversations, over the capabilities this process composed.

    Takes the capability set rather than settings so the interface and the chat
    share one store -- two `build_capabilities` calls would open independent
    connections rather than sharing the session's store object.

    A conversation may be continued by any of `runners`, including one that did
    not start it: we hold the transcript, so whichever answers next is handed
    everything the other one said and did.
    """
    return AgentSession(
        capabilities,
        runners=runners,
        workspace_repository=repository,
    )


__all__ = [
    "Settings",
    "build_capabilities",
    "build_runners",
    "build_workflow_runners",
    "build_session",
]
