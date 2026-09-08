"""Invocation-bound workflow tools for graph ACP nodes."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass

from engine.domain import AgentId, AgentRunId, StepId, StepSpec
from engine.domain.ids import WorkspaceId
from engine.ports import ApprovalHandler, SourceControl
from engine.runtime.terminal_mcp import TerminalMcpBroker, TerminalResultRegistry

from engine.graph_runtime_langgraph.executions import NodeExecution

WORKSPACE_ID = "workspaceId"


@dataclass(frozen=True, slots=True)
class TerminalMcpServer:
    """Create one existing terminal broker for each graph node invocation."""

    step_id: str
    agent_id: str
    required_outputs: tuple[str, ...] = ()
    repository_tools: tuple[str, ...] = (
        "git_subcommand",
        "open_pull_request",
    )
    source_control: SourceControl | None = None
    workspace_key: str = WORKSPACE_ID

    @asynccontextmanager
    async def __call__(
        self,
        state: Mapping[str, object],
        execution: NodeExecution,
        approve: ApprovalHandler,
    ) -> AsyncIterator[Mapping[str, object]]:
        source_control = self.source_control or execution.runtime.source_control
        if source_control is None:
            raise RuntimeError(
                "this graph's workflow tools need a SourceControl bound to its runtime"
            )
        workspace = state.get(self.workspace_key)
        if not isinstance(workspace, str) or not workspace.strip():
            raise RuntimeError(
                f"this graph's workflow tools need state[{self.workspace_key!r}] "
                "from an upstream WorkspaceNode"
            )
        broker = TerminalMcpBroker(
            run_id=execution.run_id,
            agent_run_id=AgentRunId(
                f"{execution.run_id}:{execution.execution_id}"
            ),
            step=StepSpec(
                StepId(self.step_id),
                AgentId(self.agent_id),
                self.required_outputs,
            ),
            registry=TerminalResultRegistry(),
        )
        broker.enable_repository_tools(
            source_control,
            self.repository_tools,
            WorkspaceId(workspace),
            approve,
        )
        async with broker:
            config = broker.config
            yield {
                "name": config.name,
                "command": config.command,
                "args": list(config.args),
            }


__all__ = ["TerminalMcpServer", "WORKSPACE_ID"]
