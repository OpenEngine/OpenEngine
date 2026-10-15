"""Engine's sandbox-backed ACP provider; the ACP library knows no Engine types.

Each connection owns a disposable sandbox. This is intentionally not durable
agent-session storage or an MCP bridge. Workspace exports remain explicit.
"""

import asyncio
from collections.abc import Mapping, Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass, field, replace
from pathlib import Path

from engine.ports import Sandbox, SandboxProcess, SandboxSpec
from langgraph_acp import (
    ACPAgentProvider, ACPClient, ACPConnectContext, ACPConnectionError,
    ACPElicitationHandler, ACPPermissionHandler, ClaudeACPProvider,
    CodexACPProvider, OpenCodeACPProvider, StdioACPProvider,
    connect_over_transport,
)
from langgraph_acp.agent import launch_command


@dataclass(frozen=True, slots=True, kw_only=True)
class SandboxConnectContext(ACPConnectContext):
    """The invocation's sandbox configuration, never stored in a continuation."""

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

    def __post_init__(self) -> None:
        object.__setattr__(self, "command", launch_command(self.command))

    async def connect(self, *, context: ACPConnectContext | None = None) -> ACPClient:
        if context is None or not context.workspace:
            message = "sandbox ACP requires a connection context with a workspace"
            raise ACPConnectionError(
                message,
                agent=self.name, operation="connect",
            )
        workspace = Path(context.workspace).resolve()
        spec = context.sandbox_spec if isinstance(context, SandboxConnectContext) else SandboxSpec()
        if spec.workspace is not None and spec.workspace.resolve() != workspace:
            message = "sandbox spec workspace differs from the node workspace"
            raise ACPConnectionError(
                message,
                agent=self.name, operation="connect",
            )
        spec = replace(spec, workspace=workspace, secrets={**self.env, **spec.secrets}, labels={
            **spec.labels,
            **{key: value for key, value in {
                "run": context.run_id, "node": context.node_id,
                "execution": context.execution_id,
            }.items() if value is not None},
        })
        owner = AsyncExitStack()
        try:
            sandbox = await owner.enter_async_context(self.backend.create(spec))
            cwd = await sandbox.working_directory()
            # Treat all provider values as potentially sensitive. SmolVM passes
            # secrets by temporary environment reference, never CLI plaintext.
            process = await sandbox.exec(self.command)
            return await connect_over_transport(
                agent=self.name, transport=_SandboxTransport(process, owner),
                permissions=self.permissions, elicitations=self.elicitations,
                session_cwd=cwd, allow_session_load=False,
            )
        except BaseException as primary:
            # Entry / exec can fail before ownership passes to the ACP client.
            # Preserve that failure while still waiting for sandbox teardown.
            cleanup = asyncio.create_task(owner.aclose())
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    continue
            try:
                cleanup.result()
            except Exception as error:  # noqa: BLE001 -- cleanup must not replace the original connection failure
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
    )
