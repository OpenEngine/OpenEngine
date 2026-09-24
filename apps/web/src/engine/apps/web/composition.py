"""Composition root for the web control interface.

The one file in this app allowed to name concrete adapters, and a sibling of the
other two compositions rather than shared code with them -- for the same reason
the worker's is: these processes will diverge, and sharing now would couple
three deployables that should be free to move independently.

Three of the six capabilities here are real. `agent_runner` reaches Codex or
Claude over ACP, through the same `langgraph-acp` providers the graph workflows
use, `state_store` persists conversations in SQLite, and `workspace_provider`
gives every chat an isolated Git worktree. The other three remain wired for the
composition report but are not exposed by the chat API.

`Capabilities` holds one runner because a port has one implementation, and that
is the one anything non-interactive uses. The interface additionally offers a
*choice* of runner, which is `build_runners` -- a name-to-implementation mapping
of exactly the kind a composition root exists to own.

The state store is SQLite rather than Postgres: conversations survive a process
restart without requiring an external database service.
"""

import logging
from collections.abc import Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from pathlib import Path

from langgraph_acp.providers import CLAUDE_ACP_COMMAND, CODEX_ACP_COMMAND

from engine.adapters.agent_runner.acp import (
    READ_ONLY_TOOLS,
    allowed_tools_for,
    claude_acp_runner,
    claude_session_config,
    codex_acp_runner,
)
from engine.adapters.communications.slack import (
    SlackCommunications,
    SlackCredentialStore,
)
from engine.adapters.source_control.github import GitHubSourceControl
from engine.adapters.source_control.github.transports import (
    GitHubCliTransport,
)
from engine.adapters.source_control.gitlab import GitLabSourceControl
from engine.adapters.source_control.gitlab.transports import GitLabOAuthTransport
from engine.adapters.state_store.sqlite import SQLiteStateStore
from engine.adapters.workflow_runtime.temporal import TemporalWorkflowRuntime
from engine.adapters.workspace_provider.git_worktree import (
    DEFAULT_ROOT_DIRECTORY,
    GitWorktreeWorkspaceProvider,
)
from engine.apps.web.gitlab_auth import (
    GitLabAuthError,
    GitLabCredentialStore,
    GitLabRefreshTokenInvalidError,
)
from engine.apps.web.gitlab_auth import (
    refresh_access_token as refresh_gitlab_access_token,
)
from engine.apps.web.github_webhook import GitHubWebhookConfig
from engine.apps.web.source_control import (
    RoutingSourceControl,
    SourceControlPreferences,
)
from engine.graph_runtime import GraphRuntime, GraphWorkflow
from engine.graph_runtime_langgraph.workflows import sqlite_runtime
from engine.ports import AgentRunner, Communications, SourceControl
from engine.runtime import (
    PLANNING_TOOL_NAMES,
    AgentSession,
    Capabilities,
    EngineConfig,
    PlanningMcpBroker,
    project_chat_capabilities,
)
from engine.scoper import MilestoneScoper, codex_milestone_scoper


@dataclass(frozen=True, slots=True)
class Settings:
    """Everything the interface needs from the environment.

    `host` and `port` are handed to Uvicorn by `__main__`; the rest are adapter
    arguments. Loading them from the environment lands with the deployment
    ticket, along with the other two roots.

    Frozen so one immutable settings value can be shared by the server wiring.
    """

    host: str = "localhost"
    port: int = 4364
    codex_binary: str = "codex"
    """The Codex CLI milestone scoping runs. Chat's runners reach Codex through
    `codex_acp_command`, whose adapter brings its own."""
    codex_acp_command: tuple[str, ...] = CODEX_ACP_COMMAND
    """The ACP adapter chat's Codex runners launch."""
    codex_sandbox: str = "read-only"
    """What a turn nobody is watching may do: read, and nothing else.

    `build_capabilities` wires the one runner a non-interactive caller reaches
    for, so it gets the sandbox that needs no one present. Chat is the other
    case and takes `interactive_codex_sandbox`. codex-acp cannot be asked for a
    sandbox, so `codex_acp_runner` enforces it under the adapter.
    """
    codex_working_directory: str = "."
    codex_timeout_seconds: float | None = None
    """No ceiling: a turn runs until it is done or someone cancels it."""
    codex_model: str = ""
    claude_acp_command: tuple[str, ...] = CLAUDE_ACP_COMMAND
    """The ACP adapter chat's Claude runners launch."""
    claude_working_directory: str = "."
    claude_timeout_seconds: float | None = None
    """Same as `codex_timeout_seconds`."""
    claude_model: str = ""
    interactive_codex_sandbox: str = "workspace-write"
    """An approved command has to be able to do the thing it was approved for.

    The read-only sandbox would refuse the write after the user allowed it, and
    on-request approval is what keeps that from meaning "unattended": Codex
    stops and asks before stepping outside the worktree.

    So this is not built from `approvals.allow`, and cannot be: a capability
    that policy leaves unruled is one a person may still allow mid-turn, and a
    sandbox narrowed before the turn started would refuse what they just
    allowed. Codex's policy is applied to its requests instead.
    """
    temporal_host: str = "localhost:7233"
    github_token: str = ""
    github_client_id: str = ""
    github_webhook: GitHubWebhookConfig | None = None
    """Which repository's webhook deliveries are answered, and their secret.

    ``None`` when the deployment named neither, which is what leaves the
    webhook route refusing deliveries instead of trusting unsigned ones.
    """
    source_control_preferences: SourceControlPreferences | None = None
    workspace_root: str = DEFAULT_ROOT_DIRECTORY
    sqlite_path: str = "conversations.sqlite3"
    graph_state_directory: str = "graph-state"
    """Where a graph workflow's saved progress is kept.

    Plain English: the new graph workflows remember where they got to by
    writing two small database files. This is the folder those files go in, so
    a run that was half finished when the process stopped is still there when
    it starts again. A folder rather than a file because there are two of them,
    and the graph engine names them itself.
    """
    engine_config: EngineConfig = EngineConfig()
    """Provider-neutral settings loaded from TOML.

    `approvals` is where chat's permissions are written down: it builds the
    interactive Claude runner's tool list, and answers both runners' approval
    requests. No field here holds a second copy of it.
    """
    config_path: Path | None = None
    """The single TOML source, or ``None`` when built-in defaults are active."""


def build_capabilities(
    settings: Settings,
    slack_credential_store: SlackCredentialStore | None = None,
    gitlab_credential_store: GitLabCredentialStore | None = None,
) -> Capabilities:
    """Wire every port to its concrete implementation."""
    workspace_provider = GitWorktreeWorkspaceProvider(settings.workspace_root)
    # The host's `gh auth` login is the only credential for agent GitHub
    # actions. Browser login identifies the UI user and never reaches here;
    # neither do Settings device-flow tokens or GITHUB_TOKEN.
    logging.getLogger(__name__).info(
        "source_control composition=web github_identity=gh-cli credential=gh auth"
    )
    github = GitHubSourceControl(
        "",
        host_aliases=settings.engine_config.github.host_aliases,
        workspace_provider=workspace_provider,
        transport=GitHubCliTransport(),
    )

    def _gitlab_origin() -> str:
        return (
            settings.source_control_preferences.gitlab_origin()
            if settings.source_control_preferences is not None
            and settings.source_control_preferences.gitlab_origin()
            else "https://gitlab.com"
        )

    _gitlab_stores: dict[str, GitLabCredentialStore] = {}
    _gitlab_refresh_persistence_failed: set[str] = set()

    def _gitlab_store() -> GitLabCredentialStore:
        origin = _gitlab_origin()
        if gitlab_credential_store is not None:
            return gitlab_credential_store
        return _gitlab_stores.setdefault(origin, GitLabCredentialStore(origin))

    def _gitlab_token() -> str:
        store = _gitlab_store()
        credentials = store.get_credentials()
        return credentials.access_token if credentials else ""

    async def _refresh_gitlab_after_unauthorized(failed_token: str) -> bool:
        store = _gitlab_store()
        if store.origin in _gitlab_refresh_persistence_failed:
            return False
        async with store.refresh_lock():
            credentials = store.get_credentials()
            if credentials is None or not credentials.refresh_token:
                return False
            if credentials.access_token != failed_token:
                return True
            client_id = store.get_client_id()
            if not client_id:
                return False
            try:
                refreshed = await refresh_gitlab_access_token(
                    store.origin, client_id, credentials.refresh_token
                )
            except GitLabRefreshTokenInvalidError:
                store.delete()
                return False
            except GitLabAuthError:
                return False
            try:
                store.set_credentials(refreshed)
            except GitLabAuthError:
                _gitlab_refresh_persistence_failed.add(store.origin)
                return False
            return True

    gitlab = GitLabSourceControl(
        _gitlab_token,
        origin=_gitlab_origin,
        workspace_provider=workspace_provider,
        transport=GitLabOAuthTransport(
            _gitlab_token, _gitlab_origin, _refresh_gitlab_after_unauthorized
        ),
    )
    if settings.source_control_preferences is None:
        source_control = github
    else:
        source_control = RoutingSourceControl(
            settings.source_control_preferences,
            # Both GitHub choices use the gh CLI login for agent actions.
            github,
            github,
            gitlab,
        )
    return Capabilities(
        workflow_runtime=TemporalWorkflowRuntime(settings.temporal_host),
        source_control=source_control,
        agent_runner=codex_acp_runner(
            command=settings.codex_acp_command,
            timeout_seconds=settings.codex_timeout_seconds,
            sandbox=settings.codex_sandbox,
            working_directory=settings.codex_working_directory,
            model=settings.codex_model,
            attribution=settings.engine_config.attribution,
            workspace_provider=workspace_provider,
        ),
        communications=build_communications(settings, slack_credential_store),
        workspace_provider=workspace_provider,
        state_store=SQLiteStateStore(settings.sqlite_path),
    )


def build_communications(
    settings: Settings,
    slack_credential_store: SlackCredentialStore | None = None,
) -> Communications:
    """Build the configured communications provider."""
    provider = settings.engine_config.communications.provider
    if provider == "buzz":
        raise RuntimeError(
            "communications provider 'buzz' is not available yet; "
            "configure communications.provider = 'slack'"
        )
    return SlackCommunications(slack_credential_store or SlackCredentialStore())


def build_graph_runtime(
    settings: Settings,
    graphs: Sequence[GraphWorkflow],
    source_control: SourceControl | None = None,
) -> AbstractAsyncContextManager[GraphRuntime] | None:
    """The engine that runs graph workflows, or nothing when there are none.

    It hands back an *unopened* context manager rather than a running engine,
    because starting one opens database files that somebody then has to close.
    The web application opens it when the server starts and closes it when the
    server stops, which is the only lifetime that gets that right.

    `None` when this deployment's workflow directory holds no graphs: there is
    nothing to run, so there is no reason to open the files. The interface then
    offers no graph entries, which is what keeps a person from picking one
    that nothing here could start.
    """
    if not graphs:
        return None
    return sqlite_runtime(
        tuple(graphs),
        settings.graph_state_directory,
        source_control=source_control,
    )


def claude_session_config_for(settings: Settings) -> dict[str, object] | None:
    """The ACP session metadata that wires Engine's TOML settings to Claude.

    Translates the deployment's ``attribution`` and ``[claude] output_style``
    into the dict the ``claude-agent-acp`` adapter reads from
    ``session/new`` under ``_meta``. Returns ``None`` when every setting is at its default.
    """
    return claude_session_config(
        attribution=settings.engine_config.attribution,
        output_style=settings.engine_config.claude.output_style,
    )


def build_milestone_scoper(settings: Settings) -> MilestoneScoper:
    """Build scoping from the configured Codex executable and workspace."""
    return codex_milestone_scoper(
        binary_path=settings.codex_binary,
        working_directory=settings.codex_working_directory,
        timeout_seconds=settings.codex_timeout_seconds,
        model=settings.codex_model,
    )


def build_runners(settings: Settings) -> Mapping[str, AgentRunner]:
    """Every agent runner this process offers, by the name the interface shows.

    The one place a runner name is bound to an implementation -- below this file
    "codex" and "claude" are opaque strings, exactly like tool grants. The first
    entry is the default, so it is also what a conversation gets when nobody
    picks.

    One entry per agent: the dropdown names the agent you are talking to, not
    the transport it is driven over -- ACP, for both. Both pause for approval, because a runner that
    could only run unattended is not a second choice worth offering -- what it
    would do without asking, these do after asking.

    What Claude may do without asking is `approvals.allow` from `engine.toml`:
    a preapproved tool is one whose requests never reach the callback, and one
    left off the list is still allowed if a person allows it. Codex has no such
    list -- its pre-turn knob is a sandbox, which is a ceiling rather than a
    preapproval -- so its whole policy is applied to its requests instead. Shell
    is on neither list on purpose: a shell rule is written per command rather
    than per capability, so `Bash` reaches the callback either way and
    `approvals.bash` is applied there.
    """
    workspace_provider = GitWorktreeWorkspaceProvider(settings.workspace_root)
    return {
        "codex": codex_acp_runner(
            command=settings.codex_acp_command,
            timeout_seconds=settings.codex_timeout_seconds,
            sandbox=settings.interactive_codex_sandbox,
            working_directory=settings.codex_working_directory,
            model=settings.codex_model,
            attribution=settings.engine_config.attribution,
            workspace_provider=workspace_provider,
        ),
        "claude": claude_acp_runner(
            command=settings.claude_acp_command,
            timeout_seconds=settings.claude_timeout_seconds,
            allowed_tools=allowed_tools_for(settings.engine_config.approvals.allow),
            working_directory=settings.claude_working_directory,
            model=settings.claude_model,
            attribution=settings.engine_config.attribution,
            output_style=settings.engine_config.claude.output_style,
            workspace_provider=workspace_provider,
        ),
    }


def build_read_only_runners(settings: Settings) -> Mapping[str, AgentRunner]:
    """The runners without the tools to change anything, by provider name.

    Two callers, one property: a workflow review step, and any profile that says
    it only reads. Named for what the runners are rather than for the first
    thing that wanted them, because the planner wants them for the same reason
    the reviewer does.

    Not built from `approvals.allow`, and deliberately: being unable to write is
    a property of the work rather than a permission the deployment gets to
    widen. A policy granting `edit` is a statement about what an agent may do
    when somebody asks it to change something, not about the one asked to read.

    Withholding the tools is half of it. The other half is that a `read_only`
    profile's approvals are refused by the broker, so a policy cannot hand back
    at the pause what this withheld before the turn. For Codex the withholding
    is its read-only sandbox, which `codex_acp_runner` holds every turn to.
    """
    workspace_provider = GitWorktreeWorkspaceProvider(settings.workspace_root)
    return {
        "codex": codex_acp_runner(
            command=settings.codex_acp_command,
            timeout_seconds=settings.codex_timeout_seconds,
            sandbox=settings.codex_sandbox,
            working_directory=settings.codex_working_directory,
            model=settings.codex_model,
            attribution=settings.engine_config.attribution,
            workspace_provider=workspace_provider,
        ),
        "claude": claude_acp_runner(
            command=settings.claude_acp_command,
            timeout_seconds=settings.claude_timeout_seconds,
            allowed_tools=READ_ONLY_TOOLS,
            tools=READ_ONLY_TOOLS,
            working_directory=settings.claude_working_directory,
            model=settings.claude_model,
            attribution=settings.engine_config.attribution,
            output_style=settings.engine_config.claude.output_style,
            workspace_provider=workspace_provider,
        ),
    }


def build_session(
    capabilities: Capabilities,
    runners: Mapping[str, AgentRunner],
    repository: str = ".",
    read_only_runners: Mapping[str, AgentRunner] | None = None,
) -> AgentSession:
    """Conversations, over the capabilities this process composed.

    Takes the capability set rather than settings so the interface and the chat
    share one store -- two `build_capabilities` calls would open independent
    connections rather than sharing the session's store object.

    A conversation may be continued by any of `runners`, including one that did
    not start it: we hold the transcript, so whichever answers next is handed
    everything the other one said and did.

    `read_only_runners` answers the agents that only read, by the same provider
    names -- so a planning conversation is the CLI the user picked, without the
    tools to change the tree it is reading.
    """
    return AgentSession(
        capabilities,
        runners=runners,
        workspace_repository=repository,
        read_only_runners=read_only_runners,
        mcp_brokers={name: PlanningMcpBroker for name in PLANNING_TOOL_NAMES},
        capability_resolver=project_chat_capabilities,
    )


__all__ = [
    "Settings",
    "build_capabilities",
    "build_read_only_runners",
    "build_runners",
    "build_session",
    "claude_session_config_for",
]
