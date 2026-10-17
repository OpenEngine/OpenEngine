"""Engine's sandbox-backed ACP provider; the ACP library knows no Engine types.

Each connection owns a disposable sandbox. The graph runtime supplies trusted terminal
MCP bridge capabilities and owns workspace patch acceptance separately from
this transport's lifecycle. Durable agent-session storage remains separate.
"""

import asyncio
from collections.abc import Mapping, Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass, field, replace
from pathlib import Path

from engine.ports import Sandbox, SandboxProcess, SandboxSpec
from engine.ports.sandbox_tasks import settle_task
from engine.runtime.terminal_mcp_bridge import TerminalMcpBridge
from engine.runtime.model_credentials import ModelCredentialError, ModelCredentialSource, ModelProvider
from engine.runtime.credential_rotation import credential_lease
from langgraph_acp import (
    ACPAgentProvider, ACPClient, ACPConnectContext, ACPConnectionError,
    ACPElicitationHandler, ACPPermissionHandler, ClaudeACPProvider,
    CodexACPProvider, OpenCodeACPProvider, StdioACPProvider,
    connect_over_transport,
)
from langgraph_acp.agent import launch_command
from .sandbox_sessions import AgentSessions
from .sandbox_lifecycle import AgentSandbox
from .sandbox_launch import (
    PROVIDER_ALLOWED_HOSTS as PROVIDER_ALLOWED_HOSTS,
    prepare_launch_spec, with_bridges, with_credential, with_sessions,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class SandboxConnectContext(ACPConnectContext):
    """The invocation's sandbox configuration, never stored in a continuation."""

    session_key: str = ""
    sandbox_spec: SandboxSpec = field(default_factory=SandboxSpec, repr=False)


class _SandboxTransport:
    def __init__(self, process: SandboxProcess, owner: AsyncExitStack) -> None:
        self.stdin, self.stdout, self.stderr = process.stdin, process.stdout, process.stderr
        self._process, self._owner = process, owner
        self._returncode: int | None = None

    @property
    def returncode(self) -> int | None:
        return self._returncode

    async def wait(self) -> int:
        self._returncode = await self._process.wait()
        return self._returncode

    async def close(self) -> None:
        # StdioACPClient serializes and shields close, including repeated cancellation.
        await self._owner.aclose()


@dataclass(frozen=True, slots=True, kw_only=True)
class SandboxACPProvider:
    """Launch an ACP command inside a sandbox using per-invocation context.

    `command` names a guest executable, not a host npx installation. Register
    this provider directly for custom agents. Built-in providers can be adapted
    by `sandbox_provider` when the graph runtime selects a sandbox backend.
    """

    name: str
    command: Sequence[str]
    backend: Sandbox
    env: Mapping[str, str] = field(default_factory=dict, repr=False)
    permissions: ACPPermissionHandler | None = None
    elicitations: ACPElicitationHandler | None = None
    model_provider: ModelProvider | None = None
    credential_source: ModelCredentialSource | None = field(default=None, repr=False)
    mcp_bridges: tuple[TerminalMcpBridge, ...] = field(default=(), repr=False)
    sessions: AgentSessions | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "command", launch_command(self.command))
        if self.model_provider not in {None, "codex", "claude"}:
            message = "unsupported model credential provider"
            raise ModelCredentialError(message)

    def _launch_spec(self, context: ACPConnectContext | None) -> SandboxSpec:
        if context is None or not context.workspace:
            message = "sandbox ACP requires a connection context with a workspace"
            raise ACPConnectionError(message, agent=self.name, operation="connect")
        spec = context.sandbox_spec if isinstance(context, SandboxConnectContext) else SandboxSpec()
        if spec.workspace is not None:
            spec = replace(spec, workspace=spec.workspace.resolve())
        return prepare_launch_spec(spec, agent=self.name, workspace=Path(context.workspace).resolve(),
            env=self.env, provider=self.model_provider, labels={
                key: value for key, value in {
                    "run": context.run_id, "node": context.node_id, "execution": context.execution_id,
                }.items() if value is not None
            })

    async def connect(self, *, context: ACPConnectContext | None = None) -> ACPClient:
        spec = self._launch_spec(context)
        session_key = None
        if self.sessions is not None and self.model_provider is not None:
            if not isinstance(context, SandboxConnectContext) or not context.run_id:
                message = "persistent sandbox sessions require a run context"
                raise ValueError(message)
            session_key = (context.run_id, context.session_key or context.node_id or "")
        owner = AsyncExitStack()
        try:
            if self.model_provider is not None:
                credential, state = await owner.enter_async_context(credential_lease(
                    self.credential_source, self.model_provider, spec.secrets,
                ))
                spec = with_credential(spec, credential, state)
            spec = with_bridges(spec, [(bridge.home(), bridge.socket) for bridge in self.mcp_bridges])
            if session_key is not None and self.sessions is not None and self.model_provider is not None:
                checkpoint = await owner.enter_async_context(self.sessions.hold(*session_key, self.model_provider))
                spec = with_sessions(spec, checkpoint)
            sandbox = await owner.enter_async_context(self.backend.create(spec))
            if (spec.sessions is not None or (spec.home is not None and spec.home.credentials is not None)) and not isinstance(sandbox, AgentSandbox):
                sandbox = AgentSandbox(sandbox)
                owner.push_async_callback(sandbox.stop)
            cwd = await sandbox.working_directory()
            # Treat all provider values as potentially sensitive. SmolVM passes
            # secrets by temporary environment reference, never CLI plaintext.
            if spec.home is not None and spec.home.credentials is not None:
                spec.home.credentials.exposed = True
            process = await sandbox.exec(self.command)
            return await connect_over_transport(
                agent=self.name, transport=_SandboxTransport(process, owner),
                permissions=self.permissions, elicitations=self.elicitations,
                session_cwd=cwd, allow_session_load=spec.sessions is not None,
            )
        except BaseException as primary:
            # Entry / exec can fail before ownership passes to the ACP client.
            # Preserve that failure while still waiting for sandbox teardown.
            try:
                await settle_task(asyncio.create_task(owner.aclose()))
            except (Exception, asyncio.CancelledError) as error:  # noqa: BLE001 -- preserve the original connection failure
                primary.add_note(f"sandbox cleanup failed: {error}")
            raise


def sandbox_provider(provider: ACPAgentProvider, backend: Sandbox) -> ACPAgentProvider:
    """Adapt launch configuration, retaining agent identity and callback policies."""
    if isinstance(provider, SandboxACPProvider):
        return replace(provider, backend=backend)
    if not isinstance(provider, (StdioACPProvider, CodexACPProvider, ClaudeACPProvider, OpenCodeACPProvider)):
        message = "provider has no stdio launch configuration; register a SandboxACPProvider"
        raise ACPConnectionError(
            message,
            agent=provider.name, operation="connect",
        )
    command = provider.command
    # Use image-installed commands only for the built-in default launchers.
    # Custom commands are explicit guest commands, never rewritten by name.
    if isinstance(provider, CodexACPProvider) and tuple(command) == tuple(CodexACPProvider().command):
        command = ("codex-acp",)
    elif isinstance(provider, ClaudeACPProvider) and tuple(command) == tuple(ClaudeACPProvider().command):
        command = ("claude-agent-acp",)
    elif isinstance(provider, OpenCodeACPProvider) and tuple(command) == tuple(OpenCodeACPProvider().command):
        message = "the bundled sandbox image does not include OpenCode; configure a guest command and image"
        raise ACPConnectionError(
            message,
            agent=provider.name, operation="connect",
        )
    if provider.cwd is not None:
        message = "sandbox launch cwd comes from the node workspace; remove the provider's host cwd"
        raise ACPConnectionError(
            message,
            agent=provider.name, operation="connect",
        )
    return SandboxACPProvider(
        name=provider.name, command=command, backend=backend, env=provider.env or {},
        permissions=provider.permissions, elicitations=provider.elicitations,
        model_provider="codex" if isinstance(provider, CodexACPProvider) else "claude" if isinstance(provider, ClaudeACPProvider) else None,
    )
