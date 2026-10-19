"""Invocation-bound workflow tools for graph ACP nodes."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from engine.domain import (
    AgentId, AgentRunId, ForgeMode, StepCompleted, StepId, StepSpec, forge_mode,
)
from engine.domain.ids import WorkspaceId
from engine.graph_runtime import RunStatus, UnknownGraphError, UnknownRunError
from engine.ports import ApprovalHandler, SourceControl
from engine.runtime.notifications import work_order_url
from engine.runtime.terminal_mcp import (
    LOCAL_REPOSITORY_TOOLS,
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
    """Create one existing terminal broker for each graph node invocation.

    A disconnected run (see `engine.domain.forge`) is served only the
    repository tools that stay in its checkout, and is not asked for a pull
    request: what would reach the forge is withheld by the server, not only by
    the prompt asking the agent not to.
    """

    step_id: str
    agent_id: str
    required_outputs: tuple[str, ...] = ()
    repository_tools: tuple[str, ...] = (
        "git_subcommand",
        "open_pull_request",
    )
    source_control: SourceControl | None = None
    workspace_key: str = WORKSPACE_ID
    # Raise ValueError to return a correctable tool error before acceptance.
    validate_completion: Callable[[StepCompleted], None] | None = None
    create_workorder: bool = False

    def __call__(
        self,
        state: Mapping[str, object],
        execution: NodeExecution,
        approve: ApprovalHandler,
    ) -> AbstractAsyncContextManager[BoundMcpServer]:
        return self.for_mode(forge_mode(state.get("inputs")))._serve(
            state, execution, approve,
        )

    def for_mode(self, mode: ForgeMode) -> TerminalMcpServer:
        """This server as a run in `mode` is served it."""
        if mode is ForgeMode.CONNECTED:
            return self
        return replace(
            self,
            repository_tools=tuple(
                name for name in self.repository_tools if name in LOCAL_REPOSITORY_TOOLS
            ),
            required_outputs=tuple(
                name for name in self.required_outputs if name != "pr_url"
            ),
        )

    @asynccontextmanager
    async def _serve(
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
            work_order_url=work_order_url(execution.runtime.public_url, execution.run_id),
            agent_run_id=AgentRunId(
                f"{execution.run_id}:{execution.execution_id}"
            ),
            step=StepSpec(
                StepId(self.step_id),
                AgentId(self.agent_id),
                self.required_outputs,
            ),
            registry=TerminalResultRegistry(),
            validate_completion=self.validate_completion,
        )
        if isinstance(state.get("issue"), dict):
            broker.enable_issue(state["issue"])  # pyright: ignore[reportArgumentType]  # Baseline: see docs/pyright.md
        if self.create_workorder and execution.runtime.workorder_creator is not None:
            broker.enable_workorder_creation(execution.runtime.workorder_creator)
        served = tuple(
            name
            for name in self.repository_tools
            if callable(
                getattr(source_control, REPOSITORY_TOOL_METHODS.get(name, ""), None)
            )
        )
        broker.disconnected = forge_mode(state.get("inputs")) is ForgeMode.DISCONNECTED
        broker.enable_repository_tools(
            source_control,
            served,
            WorkspaceId(workspace),
            approve,
        )
        if "add_comment" in served or "pr_url" in self.required_outputs:
            store = execution.runtime.store

            async def owned() -> tuple[tuple[str, int], ...]:
                return await store.pull_requests(execution.run_id)

            broker.enable_pull_request_ownership(owned)
        if "pr_url" in self.required_outputs:
            store = execution.runtime.store

            async def claim_reported(reported: OpenedPullRequest) -> bool:
                replacing = await store.run_for_pull_request(reported.repository, reported.number)
                if replacing is not None and replacing != execution.run_id:
                    try:
                        snapshot = await execution.runtime.snapshot(replacing)
                    except UnknownGraphError:
                        # The holder still exists, but its status is unknown.
                        return False
                    except UnknownRunError:
                        snapshot = None
                    if snapshot is not None and snapshot.status not in (
                        RunStatus.COMPLETED, RunStatus.FAILED,
                    ):
                        return False
                holder = await store.claim_pull_request(
                    PullRequestRecord(
                        repository=reported.repository,
                        number=reported.number,
                        run_id=execution.run_id,
                        opened_at=datetime.now(UTC).isoformat(),
                        node_id=execution.node_id,
                        url=reported.url,
                    ),
                    replacing=replacing,
                )
                return holder == execution.run_id

            broker.enable_pull_request_claims(claim_reported)
        if "add_comment" in served:
            store = execution.runtime.store
            # Read submission metadata, not a PR discovered in mutable graph state.
            run = await store.run(execution.run_id)
            target = (run.metadata.inputs or {}).get("pr_url") if run else None
            if target:
                broker.enable_pull_request_comments(target)

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
            store = execution.runtime.store

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
