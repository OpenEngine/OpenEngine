"""Static conformance checks; no adapters are constructed or run.

Keep every adapter and its supported port extensions here. Pyright checks
assignability of instances, including parameter names and return types.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from engine.adapters.agent_runner.acp import ACPAgentRunner
    from engine.adapters.agent_runner.acp.permissions import ACPPermissionTranslator
    from engine.adapters.communications.buzz import BuzzCommunications
    from engine.adapters.communications.slack import SlackCommunications
    from engine.adapters.sandbox.process import (
        ProcessExecution,
        ProcessSandbox,
        ProcessSandboxInstance,
    )
    from engine.adapters.source_control.github import GitHubSourceControl
    from engine.adapters.source_control.gitlab import GitLabSourceControl
    from engine.adapters.state_store.memory import InMemoryStateStore
    from engine.adapters.state_store.postgres import PostgresStateStore
    from engine.adapters.state_store.sqlite import SQLiteStateStore
    from engine.adapters.workflow_runtime.temporal import TemporalWorkflowRuntime
    from engine.adapters.workspace_provider.git_worktree import GitWorktreeWorkspaceProvider
    from engine.ports import (
        AgentRunner,
        Communications,
        InteractiveAgentRunner,
        InteractiveMcpAgentRunner,
        McpAgentRunner,
        PermissionTranslator,
        Sandbox,
        SandboxInstance,
        SandboxProcess,
        SourceControl,
        StateStore,
        StreamingAgentRunner,
        StreamingMcpAgentRunner,
        WorkflowRuntime,
        WorkspaceProvider,
    )

    def check_adapters(
        acp: ACPAgentRunner,
        permissions: ACPPermissionTranslator,
        buzz: BuzzCommunications,
        slack: SlackCommunications,
        process: ProcessExecution,
        sandbox: ProcessSandbox,
        sandbox_instance: ProcessSandboxInstance,
        github: GitHubSourceControl,
        gitlab: GitLabSourceControl,
        memory: InMemoryStateStore,
        postgres: PostgresStateStore,
        sqlite: SQLiteStateStore,
        temporal: TemporalWorkflowRuntime,
        worktree: GitWorktreeWorkspaceProvider,
    ) -> None:
        _agent_runner: AgentRunner = acp
        _mcp_runner: McpAgentRunner = acp
        _streaming_runner: StreamingAgentRunner = acp
        _streaming_mcp_runner: StreamingMcpAgentRunner = acp
        _interactive_runner: InteractiveAgentRunner = acp
        _interactive_mcp_runner: InteractiveMcpAgentRunner = acp
        _permission_translator: PermissionTranslator = permissions
        _communications: tuple[Communications, ...] = (buzz, slack)
        _sandbox_process: SandboxProcess = process
        _sandbox_port: Sandbox = sandbox
        _sandbox_instance_port: SandboxInstance = sandbox_instance
        _source_controls: tuple[SourceControl, ...] = (github, gitlab)
        _state_stores: tuple[StateStore, ...] = (memory, postgres, sqlite)
        _workflow_runtime: WorkflowRuntime = temporal
        _workspace_provider: WorkspaceProvider = worktree
