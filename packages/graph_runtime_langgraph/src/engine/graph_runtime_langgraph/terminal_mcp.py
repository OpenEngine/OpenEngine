"""Invocation-bound workflow tools for graph ACP nodes."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime

from engine.domain import AgentId, AgentRunId, StepId, StepSpec
from engine.domain.ids import WorkspaceId
from engine.ports import ApprovalHandler, SourceControl
from engine.runtime.terminal_mcp import (
    REPOSITORY_TOOL_METHODS,
    OpenedPullRequest,
    PostedComment,
    TerminalMcpBroker,
    TerminalResultRegistry,
)

from engine.graph_runtime_langgraph.acp import BoundMcpServer
from engine.graph_runtime_langgraph.executions import NodeExecution
from engine.graph_runtime_langgraph.store import CommentRecord, PullRequestRecord

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
    ) -> AsyncIterator[BoundMcpServer]:
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
        served = tuple(
            name
            for name in self.repository_tools
            if callable(
                getattr(source_control, REPOSITORY_TOOL_METHODS.get(name, ""), None)
            )
        )
        broker.enable_repository_tools(
            source_control,
            served,
            WorkspaceId(workspace),
            approve,
        )
        store = execution.runtime.store
        if "add_comment" in served:
            async def record(posted: PostedComment) -> None:
                await store.remember_comment(
                    CommentRecord(
                        comment_id=posted.result.id,
                        repository=posted.repository,
                        kind=posted.kind,
                        pr_number=posted.pr_number,
                        run_id=execution.run_id,
                        posted_at=datetime.now(UTC).isoformat(),
                        node_id=execution.node_id,
                        url=posted.result.url,
                    )
                )

            broker.enable_comment_records(record)
        if "open_pull_request" in served:
            async def claim(opened: OpenedPullRequest) -> None:
                await store.remember_pull_request(
                    PullRequestRecord(
                        repository=opened.repository,
                        number=opened.number,
                        run_id=execution.run_id,
                        opened_at=datetime.now(UTC).isoformat(),
                        node_id=execution.node_id,
                        url=opened.url,
                    )
                )

            broker.enable_pull_request_records(claim)

        async def owned() -> tuple[str, ...]:
            records = await store.pull_requests(execution.run_id)
            return tuple(record.url for record in records if record.url)

        broker.enable_pull_request_ownership(owned)
        async with broker:
            config = broker.config
            yield BoundMcpServer(
                config={
                    "name": config.name,
                    "command": config.command,
                    "args": list(config.args),
                    # Required by ACP even when it is empty, and an agent that
                    # validates `session/new` against the schema -- claude does
                    # -- refuses the whole session without it rather than
                    # defaulting it. The broker's credentials travel in `args`,
                    # so there is nothing to put here.
                    "env": [],
                },
                result=broker.result,
                clarification=broker.clarification,
            )


__all__ = ["TerminalMcpServer", "WORKSPACE_ID"]
