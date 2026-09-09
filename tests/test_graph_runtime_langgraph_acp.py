"""ACP agents under the graph runtime, including losing the process that ran one.

`tests/test_graph_runtime.py` already drives the whole control surface against
this binding -- linear graphs, fan-out, the same node running three times at
once, checkpoint history, fork and resume, shutdown. What is here is the part
that needs a real agent on the other end of a real pipe:

* steering an ACP execution without it becoming a second conversation;
* an approval request that outlives the Python task that raised it;
* the same, when the runtime is destroyed and rebuilt from files in between;
* a refusal;
* several agents asking at once, each answered separately;
* durability backed by a LangGraph SQLite checkpointer rather than a dict.

The agent is `tests/acp_stub_agent.py`, launched as a child process. It keeps
its own conversation on disk and remembers an unanswered permission request
across a reload, which is what a real agent mid-tool-call does -- and what makes
a broken handoff fail here rather than pass quietly.

Every durability test writes to `tmp_path` and rebuilds *everything* from it: a
new `LangGraphRuntime`, a new checkpointer connection, a new store, and a new
agent process. Nothing but the files crosses the boundary, which is the only
version of this test worth having.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from engine.domain import ApprovalDecision, ApprovalId, ApprovalKind, RunId, WorkspaceId
from engine.graph_runtime import EventLog, GraphId, NodeId, RuntimeEvent
from engine.graph_runtime_langgraph import (
    LangGraphDefinition,
    LangGraphRuntime,
    SqliteGraphRuntimeStore,
    TerminalMcpServer,
    answer_permission,
)
from engine.ports import (
    ChangeRequest,
    GitResult,
    JobLogs,
    PipelineRetry,
    PipelineStatus,
    SourceControl,
    WorkItem,
)
from engine.graph_runtime_langgraph.acp import (
    APPROVAL_ID, ACPNode, _replay_history, _replay_prompt,
)
from engine.graph_runtime.events import EventKind
from engine.runtime import INVALID_COMPLETION_ERROR
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph_acp import ACPAgentRegistry, StdioACPProvider

from acp_stub_agent import ASK_NARRATION, DONE, NARRATED_TOOL, NARRATION
from graph_runtime_backends import State

STUB = Path(__file__).parent / "acp_stub_agent.py"
GRAPH = GraphId("acp-review")
IMPLEMENTATION = NodeId("implementation")
REVIEW = NodeId("review")
AGENT = "stub"
PROMPT = "Implement the feature and run the tests."
WORKFLOW_MCP_SERVER: Mapping[str, Any] = {
    "name": "workflow",
    "command": sys.executable,
    "args": ["-m", "engine.runtime.terminal_mcp_server"],
}

#: Long enough that only a genuinely stuck run reaches it. A passing run never
#: waits, but a child process and two SQLite files make this slower than the
#: in-memory suite.
PATIENCE = 30.0


def registry(
    tmp_path: Path,
    *,
    asks: bool = False,
    asks_every: bool = False,
    response: str = DONE,
    narrates: bool = False,
    waits_for_cancel: bool = False,
    uses_mcp: bool = False,
    mcp_git: bool = False,
    mcp_terminal: str = "",
    mcp_omit_outputs: bool = False,
    tool_call: Mapping[str, Any] | None = None,
    mcp_clarify: bool = False,
    mcp_clarify_on_correction: bool = False,
    mcp_terminal_on_correction: bool = False,
    mcp_terminal_after_grant: bool = False,
) -> ACPAgentRegistry:
    """One stub agent, reachable as `"stub"`, answering through the runtime."""
    return ACPAgentRegistry(
        [
            StdioACPProvider(
                name=AGENT,
                command=[sys.executable, str(STUB)],
                env={
                    "STUB_ACP_STATE": str(tmp_path),
                    "STUB_ACP_LOG": str(tmp_path / "agent.log"),
                    "STUB_ACP_RESPONSE": response,
                    **({"STUB_ACP_TOOL_CALL": json.dumps(tool_call)} if tool_call else {}),
                    **({"STUB_ACP_ASK": "1"} if asks or asks_every else {}),
                    **({"STUB_ACP_ASK_EVERY": "1"} if asks_every else {}),
                    **({"STUB_ACP_NARRATE": "1"} if narrates else {}),
                    **({"STUB_ACP_WAIT_FOR_CANCEL": "1"} if waits_for_cancel else {}),
                    **({"STUB_ACP_USE_MCP": "1"} if uses_mcp else {}),
                    **({"STUB_ACP_MCP_GIT": "1"} if mcp_git else {}),
                    **(
                        {"STUB_ACP_MCP_TERMINAL": mcp_terminal}
                        if mcp_terminal
                        else {}
                    ),
                    **(
                        {"STUB_ACP_MCP_OMIT_OUTPUTS": "1"}
                        if mcp_omit_outputs
                        else {}
                    ),
                    **({"STUB_ACP_MCP_CLARIFY": "1"} if mcp_clarify else {}),
                    **(
                        {"STUB_ACP_MCP_CLARIFY_ON_CORRECTION": "1"}
                        if mcp_clarify_on_correction
                        else {}
                    ),
                    **(
                        {"STUB_ACP_MCP_TERMINAL_ON_CORRECTION": "1"}
                        if mcp_terminal_on_correction
                        else {}
                    ),
                    **(
                        {"STUB_ACP_MCP_TERMINAL_AFTER_GRANT": "1"}
                        if mcp_terminal_after_grant
                        else {}
                    ),
                },
                # The seam the whole design turns on: a permission request comes
                # in on the ACP connection, and this is what routes it back to
                # the execution that owns the conversation.
                permissions=answer_permission,
            )
        ]
    )


def pipeline(
    saver: Any,
    agents: ACPAgentRegistry,
    where: Path,
    *,
    mcp_servers: tuple[Mapping[str, Any], ...] = (),
    mcp_server_bindings: tuple[Any, ...] = (),
) -> LangGraphDefinition:
    """implementation -> review, with the implementation node an ACP agent."""
    builder: StateGraph = StateGraph(State)
    builder.add_node(
        str(IMPLEMENTATION),
        ACPNode(
            agent=AGENT,
            prompt=PROMPT,
            registry=agents,
            cwd=str(where),
            mcp_servers=mcp_servers,
            mcp_server_bindings=mcp_server_bindings,
        ),
    )
    builder.add_node(str(REVIEW), _reviewed)
    builder.add_edge(START, str(IMPLEMENTATION))
    builder.add_edge(str(IMPLEMENTATION), str(REVIEW))
    builder.add_edge(str(REVIEW), END)
    return LangGraphDefinition(
        graph_id=GRAPH, name="ACP review", graph=builder.compile(checkpointer=saver)
    )


def pipeline_with_workflow_mcp(
    saver: Any, agents: ACPAgentRegistry, where: Path
) -> LangGraphDefinition:
    return pipeline(saver, agents, where, mcp_servers=(WORKFLOW_MCP_SERVER,))


def pipeline_with_run_bound_mcp(
    saver: Any, agents: ACPAgentRegistry, where: Path
) -> LangGraphDefinition:
    return pipeline(
        saver,
        agents,
        where,
        mcp_server_bindings=(
            TerminalMcpServer(
                step_id=str(IMPLEMENTATION),
                agent_id=AGENT,
                required_outputs=("pr_url",),
            ),
        ),
    )


class RecordingSourceControl:
    def __init__(self) -> None:
        self.reviews: list[tuple[object, ...]] = []
        self.git_calls: list[tuple[WorkspaceId, tuple[str, ...]]] = []

    async def run_git(
        self, workspace_id: WorkspaceId, arguments: Sequence[str]
    ) -> GitResult:
        self.git_calls.append((workspace_id, tuple(arguments)))
        return GitResult(0, "working tree clean", "")

    async def request_review(
        self,
        workspace_id: WorkspaceId,
        branch: str,
        base_ref: str,
        title: str,
        body: str,
    ) -> str:
        self.reviews.append((workspace_id, branch, base_ref, title, body))
        return "https://github.com/acme/repository/pull/7"

    async def create_branch(
        self, _workspace_id: WorkspaceId, _name: str, _base_ref: str
    ) -> None:
        pass

    async def commit_all(self, _workspace_id: WorkspaceId, _message: str) -> str:
        return "abc123"

    async def publish(self, _workspace_id: WorkspaceId, _branch: str) -> None:
        pass

    async def add_comment(
        self,
        _pr_url: str,
        _comment: str,
        _file: str | None = None,
        _line: int | None = None,
    ) -> None:
        pass

    async def view_change_request(
        self, _workspace_id: WorkspaceId, _number: int
    ) -> ChangeRequest:
        raise AssertionError("not called")

    async def list_work_items(
        self,
        _workspace_id: WorkspaceId,
        _state: str = "open",
        _labels: Sequence[str] = (),
        _limit: int = 30,
    ) -> tuple[WorkItem, ...]:
        return ()

    async def view_work_item(
        self, _workspace_id: WorkspaceId, _number: int
    ) -> WorkItem:
        raise AssertionError("not called")

    async def list_pipeline_status(
        self,
        _workspace_id: WorkspaceId,
        *,
        ref: str | None = None,
        change_request_number: int | None = None,
    ) -> PipelineStatus:
        raise AssertionError("not called")

    async def get_job_logs(
        self,
        _workspace_id: WorkspaceId,
        _pipeline_id: int,
        _job_id: int | None = None,
    ) -> JobLogs:
        raise AssertionError("not called")

    async def retry_pipeline(
        self,
        _workspace_id: WorkspaceId,
        _pipeline_id: int,
        _job_id: int | None = None,
    ) -> PipelineRetry:
        raise AssertionError("not called")


assert isinstance(RecordingSourceControl(), SourceControl)


def pool(saver: Any, agents: ACPAgentRegistry, where: Path) -> LangGraphDefinition:
    """Two ACP agents at once, which is what makes routing an answer a question."""
    builder: StateGraph = StateGraph(State)
    for node_id in ("agent-1", "agent-2"):
        builder.add_node(
            node_id,
            ACPNode(
                agent=AGENT,
                prompt=f"{PROMPT} ({node_id})",
                registry=agents,
                cwd=str(where),
            ),
        )
        builder.add_edge(START, node_id)
        builder.add_edge(node_id, END)
    return LangGraphDefinition(
        graph_id=GRAPH, name="ACP pool", graph=builder.compile(checkpointer=saver)
    )


async def _reviewed(_state: dict[str, Any]) -> dict[str, Any]:
    return {str(REVIEW): "Looks right."}


@asynccontextmanager
async def runtime_over(
    tmp_path: Path,
    agents: ACPAgentRegistry,
    build: Any = pipeline,
    source_control: Any | None = None,
) -> AsyncIterator[tuple[LangGraphRuntime, EventLog]]:
    """A runtime built entirely from what is on disk, and closed like a server.

    Everything durable is a file under `tmp_path`, so entering this twice is a
    process restart in every sense that matters: a second checkpointer
    connection, a second store, and a runtime that has never seen the run it is
    about to be asked about.
    """
    store = SqliteGraphRuntimeStore(tmp_path / "runtime.db")
    async with AsyncSqliteSaver.from_conn_string(
        str(tmp_path / "checkpoints.db")
    ) as saver:
        runtime = LangGraphRuntime(
            build(saver, agents, tmp_path),
            store=store,
            source_control=source_control,
        )
        log = EventLog()
        runtime.observe(log.append)
        try:
            yield runtime, log
        finally:
            await runtime.aclose()
            store.close()


async def until(
    log: EventLog, run_id: RunId, kind: str, count: int = 1, cursor: int = 0
) -> list[RuntimeEvent]:
    """Everything up to and including the `count`th event of `kind`."""
    seen: list[RuntimeEvent] = []
    async with asyncio.timeout(PATIENCE):
        async for event in log.stream(run_id, cursor):
            seen.append(event)
            if event.kind.value == kind:
                count -= 1
                if count == 0:
                    return seen
    raise AssertionError("unreachable")  # pragma: no cover


def transcript(events: Sequence[RuntimeEvent]) -> list[tuple[str, str]]:
    return [
        (str(event.payload["role"]), str(event.payload["text"]))
        for event in events
        if event.kind.value == "transcript"
    ]


#: Which field names the thing each kind of activity is about.
_NAMED_BY = {
    "transcript": "text",
    "tool.call": "name",
    "tool.result": "name",
    "approval.requested": "reason",
}


def activity(events: Sequence[RuntimeEvent], node_id: NodeId) -> list[tuple[str, str]]:
    """What one node did, in order: what it said, called, and asked for."""
    return [
        (event.kind.value, str(event.payload[_NAMED_BY[event.kind.value]]))
        for event in events
        if event.kind.value in _NAMED_BY and event.node_id == node_id
    ]


def sessions(tmp_path: Path) -> dict[str, dict[str, Any]]:
    """Every conversation the agent kept, as the agent left it on disk."""
    return {
        path.stem: json.loads(path.read_text())
        for path in sorted(tmp_path.glob("sess_*.json"))
    }


def sent(tmp_path: Path, method: str) -> list[dict[str, Any]]:
    """Every call of `method` the agent was sent, across every process."""
    log = tmp_path / "agent.log"
    return [
        message
        for message in (
            json.loads(line) for line in log.read_text().splitlines() if line.strip()
        )
        if message.get("method") == method
    ]


def prompts(tmp_path: Path) -> list[str]:
    return [
        "".join(
            str(block.get("text", ""))
            for block in message["params"].get("prompt", [])
            if isinstance(block, dict)
        )
        for message in sent(tmp_path, "session/prompt")
    ]


# --- an agent that just runs ------------------------------------------------


@pytest.mark.parametrize(
    ("tool_call", "expected_kind"),
    [
        ({"title": "ExitPlanMode", "kind": "other"}, ApprovalKind.PLAN_APPROVAL),
        ({"title": "AskUserQuestion", "kind": "other"}, ApprovalKind.USER_INPUT),
        ({"title": "Review the plan", "kind": "switch_mode"}, ApprovalKind.PLAN_APPROVAL),
    ],
)
@pytest.mark.parametrize("enable_before_request", [False, True])
def test_acp_human_requests_require_manual_approval(
    tmp_path: Path,
    tool_call: dict[str, str],
    expected_kind: ApprovalKind,
    enable_before_request: bool,
) -> None:
    async def scenario() -> None:
        agents = registry(
            tmp_path, asks=True, tool_call={"toolCallId": "call_1", **tool_call}
        )
        async with runtime_over(tmp_path, agents) as (runtime, log):
            run = await runtime.start(GRAPH, {})
            if enable_before_request:
                await runtime.set_auto_approve(run.run_id, IMPLEMENTATION, True)
            events = await until(log, run.run_id, "approval.requested")
            assert events[-1].payload["kind"] == expected_kind.value
            # This also exercises enabling the preference on an existing request.
            snapshot = await runtime.set_auto_approve(run.run_id, IMPLEMENTATION, True)
            assert len(snapshot.pending_approvals) == 1
            approval = snapshot.pending_approvals[0]
            assert approval.kind is expected_kind
            assert await runtime.recorded_decision(approval.approval_id) is None
            await runtime.decide(run.run_id, approval.approval_id, ApprovalDecision.ACCEPT)
            await until(log, run.run_id, "run.finished")

    asyncio.run(scenario())


def test_acp_commands_can_still_be_auto_approved(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with runtime_over(tmp_path, registry(tmp_path, asks=True)) as (runtime, log):
            run = await runtime.start(GRAPH, {})
            await runtime.set_auto_approve(run.run_id, IMPLEMENTATION, True)
            events = await until(log, run.run_id, "run.finished")
            requested = [event for event in events if event.kind.value == "approval.requested"]
            assert len(requested) == 1
            assert requested[0].payload["kind"] == ApprovalKind.COMMAND_EXECUTION.value
            assert not (await runtime.snapshot(run.run_id)).pending_approvals

    asyncio.run(scenario())


def test_an_acp_node_runs_a_turn_and_publishes_what_happened(tmp_path: Path) -> None:
    async def scenario() -> tuple[list[RuntimeEvent], dict[str, Any]]:
        async with runtime_over(tmp_path, registry(tmp_path)) as (runtime, log):
            run = await runtime.start(GRAPH, {})
            events = await until(log, run.run_id, "run.finished")
            final = await runtime.snapshot(run.run_id)
            return events, dict(final.values)

    events, values = asyncio.run(scenario())

    # Both halves of the turn: what the node was sent to do, and what it said
    # about doing it. A transcript holding only the second is not a
    # conversation, and a reader opening one has to guess what was asked.
    assert transcript(events) == [("user", PROMPT), ("assistant", DONE)]
    assert values == {str(IMPLEMENTATION): DONE, str(REVIEW): "Looks right."}
    started = [event for event in events if event.kind.value == "conversation.started"]
    assert len(started) == 1
    assert started[0].node_id == IMPLEMENTATION
    assert started[0].payload == {
        "agent": AGENT,
        "sessionId": next(iter(sessions(tmp_path))),
        "resumed": False,
    }
    # One conversation, started once. The node did not open a second.
    assert len(sent(tmp_path, "session/new")) == 1
    assert prompts(tmp_path) == [PROMPT]


def test_an_acp_node_passes_its_mcp_servers_to_a_new_session(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with runtime_over(
            tmp_path, registry(tmp_path), pipeline_with_workflow_mcp
        ) as (runtime, log):
            run = await runtime.start(GRAPH, {})
            await until(log, run.run_id, "run.finished")

    asyncio.run(scenario())

    created = sent(tmp_path, "session/new")
    assert len(created) == 1
    assert created[0]["params"]["mcpServers"] == [WORKFLOW_MCP_SERVER]


def test_run_bound_tools_cross_acp_and_mcp_processes_and_rebind_on_resume(
    tmp_path: Path,
) -> None:
    """A replacement broker reconnects one durable ACP conversation."""

    source_control = RecordingSourceControl()

    async def raise_it() -> tuple[RunId, str]:
        async with runtime_over(
            tmp_path,
            registry(
                tmp_path,
                asks=True,
                uses_mcp=True,
                mcp_terminal="complete_step",
                mcp_terminal_after_grant=True,
            ),
            pipeline_with_run_bound_mcp,
            source_control,
        ) as (runtime, log):
            run = await runtime.start(GRAPH, {"workspaceId": "ws-graph-run"})
            asked = await until(log, run.run_id, "approval.requested")
            return run.run_id, str(asked[-1].payload["approvalId"])

    async def answer_it(run_id: RunId, approval_id: str) -> None:
        async with runtime_over(
            tmp_path,
            registry(
                tmp_path,
                asks=True,
                uses_mcp=True,
                mcp_terminal="complete_step",
                mcp_terminal_after_grant=True,
            ),
            pipeline_with_run_bound_mcp,
            source_control,
        ) as (runtime, log):
            await runtime.decide(
                run_id,
                approval_id,  # type: ignore[arg-type]
                ApprovalDecision.ACCEPT,
            )
            await until(log, run_id, "run.finished")

    run_id, approval_id = asyncio.run(raise_it())
    asyncio.run(answer_it(run_id, approval_id))

    assert len(sessions(tmp_path)) == 1
    session = next(iter(sessions(tmp_path).values()))
    assert session["loads"] == 1
    assert session["mcp_tools"] == [
        [
            "complete_step",
            "fail_step",
            "clarify",
            "git_subcommand",
            "open_pull_request",
        ],
        [
            "complete_step",
            "fail_step",
            "clarify",
            "git_subcommand",
            "open_pull_request",
        ],
        [
            "complete_step",
            "fail_step",
            "clarify",
            "git_subcommand",
            "open_pull_request",
        ],
    ]
    assert len(set(session["mcp_ports"])) == 2
    assert session["mcp_review"]["result"]["structuredContent"]["output"] == (
        "https://github.com/acme/repository/pull/7"
    )
    assert source_control.reviews == [
        (
            WorkspaceId("ws-graph-run"),
            "agent/graph-tools",
            "main",
            "feat: add graph tools",
            "Test body.",
        )
    ]
    assert source_control.git_calls == []
    assert len(sent(tmp_path, "session/new")) == 1
    assert len(sent(tmp_path, "session/load")) == 1


def test_run_bound_git_keeps_the_broker_approval_boundary(tmp_path: Path) -> None:
    source_control = RecordingSourceControl()

    async def scenario() -> tuple[dict[str, Any], list[RuntimeEvent]]:
        async with runtime_over(
            tmp_path,
            registry(
                tmp_path,
                uses_mcp=True,
                mcp_git=True,
                mcp_terminal="complete_step",
            ),
            pipeline_with_run_bound_mcp,
            source_control,
        ) as (runtime, log):
            run = await runtime.start(GRAPH, {"workspaceId": "ws-graph-run"})
            events = await until(log, run.run_id, "approval.requested")
            approval_id = events[-1].payload["approvalId"]
            assert source_control.git_calls == []
            await runtime.decide(
                run.run_id,
                approval_id,  # type: ignore[arg-type]
                ApprovalDecision.ACCEPT,
            )
            events.extend(await until(log, run.run_id, "run.finished"))
            return next(iter(sessions(tmp_path).values())), events

    session, events = asyncio.run(scenario())

    requested = [event for event in events if event.kind.value == "approval.requested"]
    assert requested[-1].payload["command"] == "git status --short"
    assert requested[-1].payload["toolName"] == "mcp__workflow__git_subcommand"
    assert source_control.git_calls == [
        (WorkspaceId("ws-graph-run"), ("status", "--short"))
    ]
    assert session["mcp_git"]["result"]["structuredContent"]["output"] == (
        "working tree clean"
    )


def test_cancelling_run_bound_git_never_calls_source_control(tmp_path: Path) -> None:
    source_control = RecordingSourceControl()

    async def scenario() -> dict[str, Any]:
        async with runtime_over(
            tmp_path,
            registry(
                tmp_path,
                uses_mcp=True,
                mcp_git=True,
                mcp_terminal="complete_step",
            ),
            pipeline_with_run_bound_mcp,
            source_control,
        ) as (runtime, log):
            run = await runtime.start(GRAPH, {"workspaceId": "ws-graph-run"})
            events = await until(log, run.run_id, "approval.requested")
            await runtime.decide(
                run.run_id,
                events[-1].payload["approvalId"],  # type: ignore[arg-type]
                ApprovalDecision.CANCEL,
            )
            await until(log, run.run_id, "run.finished")
            return next(iter(sessions(tmp_path).values()))

    session = asyncio.run(scenario())

    assert source_control.git_calls == []
    assert session["mcp_git"]["result"]["isError"] is True
    assert session["mcp_git"]["result"]["content"] == [
        {"type": "text", "text": "git_subcommand was not approved"}
    ]


def test_an_acp_answer_cannot_approve_an_unrelated_mcp_request_after_resume(
    tmp_path: Path,
) -> None:
    source_control = RecordingSourceControl()

    async def raise_it() -> tuple[RunId, str]:
        async with runtime_over(
            tmp_path,
            registry(tmp_path, asks=True),
            pipeline_with_run_bound_mcp,
            source_control,
        ) as (runtime, log):
            run = await runtime.start(GRAPH, {"workspaceId": "ws-graph-run"})
            asked = await until(log, run.run_id, "approval.requested")
            return run.run_id, str(asked[-1].payload["approvalId"])

    async def answer_it(run_id: RunId, approval_id: str) -> None:
        async with runtime_over(
            tmp_path,
            registry(
                tmp_path,
                asks=True,
                uses_mcp=True,
                mcp_git=True,
                mcp_terminal="complete_step",
                mcp_terminal_after_grant=True,
            ),
            pipeline_with_run_bound_mcp,
            source_control,
        ) as (runtime, log):
            await runtime.decide(
                run_id,
                approval_id,  # type: ignore[arg-type]
                ApprovalDecision.ACCEPT,
            )
            await until(log, run_id, "run.finished")

    run_id, approval_id = asyncio.run(raise_it())
    asyncio.run(answer_it(run_id, approval_id))

    session = next(iter(sessions(tmp_path).values()))
    assert session["mcp_git"]["result"]["isError"] is True
    assert "not approved" in session["mcp_git"]["result"]["content"][0]["text"]
    assert source_control.git_calls == []
    assert session["granted"] is True


def test_complete_step_carries_declared_outputs_into_graph_state(
    tmp_path: Path,
) -> None:
    async def scenario() -> tuple[dict[str, Any], dict[str, Any]]:
        async with runtime_over(
            tmp_path,
            registry(
                tmp_path,
                uses_mcp=True,
                mcp_terminal="complete_step",
            ),
            pipeline_with_run_bound_mcp,
            RecordingSourceControl(),
        ) as (runtime, log):
            run = await runtime.start(GRAPH, {"workspaceId": "ws-graph-run"})
            await until(log, run.run_id, "run.finished")
            return (
                dict((await runtime.snapshot(run.run_id)).values),
                next(iter(sessions(tmp_path).values())),
            )

    values, session = asyncio.run(scenario())

    assert session["mcp_terminal"] == "complete_step"
    assert values[str(IMPLEMENTATION)] == "Implemented through MCP."
    assert values["pr_url"] == "https://github.com/acme/repository/pull/7"
    assert values[str(REVIEW)] == "Looks right."


def test_ordinary_turn_is_reprompted_before_the_graph_can_advance(
    tmp_path: Path,
) -> None:
    async def scenario() -> tuple[Any, list[RuntimeEvent]]:
        async with runtime_over(
            tmp_path,
            registry(tmp_path, response="I think I am done."),
            pipeline_with_run_bound_mcp,
            RecordingSourceControl(),
        ) as (runtime, log):
            run = await runtime.start(GRAPH, {"workspaceId": "ws-graph-run"})
            events = await until(log, run.run_id, "run.failed")
            return await runtime.snapshot(run.run_id), events

    final, events = asyncio.run(scenario())

    assert final.status.value == "failed"
    assert final.error == (
        "the implementation agent ended 3 turns without reporting a valid "
        "terminal result"
    )
    assert str(REVIEW) not in final.values
    assert [text for role, text in transcript(events) if role == "assistant"] == [
        "I think I am done.",
        "I think I am done.",
        "I think I am done.",
    ]
    assert len(
        [
            text
            for role, text in transcript(events)
            if role == "user" and "Valid completion states" in text
        ]
    ) == 2


def test_an_ordinary_turn_can_complete_after_the_correction(tmp_path: Path) -> None:
    async def scenario() -> dict[str, Any]:
        async with runtime_over(
            tmp_path,
            registry(
                tmp_path,
                uses_mcp=True,
                mcp_terminal="complete_step",
                mcp_terminal_on_correction=True,
            ),
            pipeline_with_run_bound_mcp,
            RecordingSourceControl(),
        ) as (runtime, log):
            run = await runtime.start(GRAPH, {"workspaceId": "ws-graph-run"})
            await until(log, run.run_id, "run.finished")
            return dict((await runtime.snapshot(run.run_id)).values)

    values = asyncio.run(scenario())

    assert prompts(tmp_path) == [PROMPT, INVALID_COMPLETION_ERROR]
    assert values["pr_url"] == "https://github.com/acme/repository/pull/7"
    assert values[str(REVIEW)] == "Looks right."


def test_clarify_preserves_the_graph_position_until_continuation(
    tmp_path: Path,
) -> None:
    async def scenario() -> tuple[Any, Any]:
        async with runtime_over(
            tmp_path,
            registry(
                tmp_path,
                uses_mcp=True,
                mcp_clarify=True,
                mcp_terminal="complete_step",
            ),
            pipeline_with_run_bound_mcp,
            RecordingSourceControl(),
        ) as (runtime, log):
            run = await runtime.start(GRAPH, {"workspaceId": "ws-graph-run"})
            await until(log, run.run_id, "transcript", count=2)
            paused = await runtime.snapshot(run.run_id)
            await runtime.steer(run.run_id, "Continue with the implementation.")
            await until(log, run.run_id, "run.finished")
            return paused, await runtime.snapshot(run.run_id)

    paused, final = asyncio.run(scenario())

    assert paused.status.value == "running"
    assert [execution.node_id for execution in paused.active_executions] == [
        IMPLEMENTATION
    ]
    assert str(IMPLEMENTATION) not in paused.values
    assert str(REVIEW) not in paused.values
    assert final.values["pr_url"] == "https://github.com/acme/repository/pull/7"
    assert final.values[str(REVIEW)] == "Looks right."


def test_clarify_resets_the_invalid_completion_budget(tmp_path: Path) -> None:
    async def scenario() -> tuple[Any, Any]:
        async with runtime_over(
            tmp_path,
            registry(
                tmp_path,
                response="I stopped without a terminal result.",
                uses_mcp=True,
                mcp_clarify_on_correction=True,
            ),
            pipeline_with_run_bound_mcp,
            RecordingSourceControl(),
        ) as (runtime, log):
            run = await runtime.start(GRAPH, {"workspaceId": "ws-graph-run"})
            await until(log, run.run_id, "transcript", count=4)
            paused = await runtime.snapshot(run.run_id)
            await runtime.steer(run.run_id, "Continue with the implementation.")
            await until(log, run.run_id, "run.failed")
            return paused, await runtime.snapshot(run.run_id)

    paused, final = asyncio.run(scenario())

    assert [execution.node_id for execution in paused.active_executions] == [
        IMPLEMENTATION
    ]
    assert prompts(tmp_path) == [
        PROMPT,
        INVALID_COMPLETION_ERROR,
        "Continue with the implementation.",
        INVALID_COMPLETION_ERROR,
        INVALID_COMPLETION_ERROR,
    ]
    assert final.error == (
        "the implementation agent ended 3 turns without reporting a valid "
        "terminal result"
    )
    assert str(REVIEW) not in final.values


def test_complete_step_rejects_a_missing_declared_output(tmp_path: Path) -> None:
    async def scenario() -> tuple[Any, dict[str, Any]]:
        async with runtime_over(
            tmp_path,
            registry(
                tmp_path,
                uses_mcp=True,
                mcp_terminal="complete_step",
                mcp_omit_outputs=True,
            ),
            pipeline_with_run_bound_mcp,
            RecordingSourceControl(),
        ) as (runtime, log):
            run = await runtime.start(GRAPH, {"workspaceId": "ws-graph-run"})
            await until(log, run.run_id, "run.failed")
            return (
                await runtime.snapshot(run.run_id),
                next(iter(sessions(tmp_path).values())),
            )

    final, session = asyncio.run(scenario())

    response = session["mcp_terminal_response"]["result"]
    assert response["isError"] is True
    assert response["content"] == [
        {
            "type": "text",
            "text": "step result is missing required outputs: pr_url",
        }
    ]
    assert final.status.value == "failed"
    assert "pr_url" not in final.values
    assert str(IMPLEMENTATION) not in final.values
    assert str(REVIEW) not in final.values


def test_fail_step_fails_the_graph_run(tmp_path: Path) -> None:
    async def scenario() -> tuple[Any, dict[str, Any]]:
        async with runtime_over(
            tmp_path,
            registry(tmp_path, uses_mcp=True, mcp_terminal="fail_step"),
            pipeline_with_run_bound_mcp,
            RecordingSourceControl(),
        ) as (runtime, log):
            run = await runtime.start(GRAPH, {"workspaceId": "ws-graph-run"})
            await until(log, run.run_id, "run.failed")
            return (
                await runtime.snapshot(run.run_id),
                next(iter(sessions(tmp_path).values())),
            )

    final, session = asyncio.run(scenario())

    assert session["mcp_terminal"] == "fail_step"
    assert final.status.value == "failed"
    assert final.error == "The implementation cannot continue."
    assert str(REVIEW) not in final.values


@pytest.mark.parametrize(
    ("state", "source_control", "message"),
    [
        (
            {"workspaceId": "ws-graph-run"},
            None,
            "this graph's workflow tools need a SourceControl bound to its runtime",
        ),
        (
            {},
            RecordingSourceControl(),
            "this graph's workflow tools need state['workspaceId'] from an upstream "
            "WorkspaceNode",
        ),
    ],
    ids=("no-source-control", "no-workspace"),
)
def test_run_bound_tools_fail_loudly_when_their_binding_is_missing(
    tmp_path: Path,
    state: dict[str, object],
    source_control: SourceControl | None,
    message: str,
) -> None:
    async def scenario() -> Any:
        async with runtime_over(
            tmp_path,
            registry(tmp_path),
            pipeline_with_run_bound_mcp,
            source_control,
        ) as (runtime, log):
            run = await runtime.start(GRAPH, state)
            await until(log, run.run_id, "run.failed")
            return await runtime.snapshot(run.run_id)

    final = asyncio.run(scenario())

    assert final.status.value == "failed"
    assert final.error == message
    assert not (tmp_path / "agent.log").exists()


def test_run_bound_tools_intersect_with_source_control_capabilities(
    tmp_path: Path,
) -> None:
    class GitOnlySourceControl:
        async def run_git(
            self, _workspace_id: WorkspaceId, _arguments: Sequence[str]
        ) -> GitResult:
            return GitResult(0, "", "")

    async def scenario() -> None:
        async with runtime_over(
            tmp_path,
            registry(tmp_path, uses_mcp=True, mcp_terminal="complete_step"),
            pipeline_with_run_bound_mcp,
            GitOnlySourceControl(),
        ) as (runtime, log):
            run = await runtime.start(GRAPH, {"workspaceId": "ws-graph-run"})
            await until(log, run.run_id, "run.finished")

    asyncio.run(scenario())

    session = next(iter(sessions(tmp_path).values()))
    assert session["mcp_tools"] == [
        ["complete_step", "fail_step", "clarify", "git_subcommand"]
    ]


def test_what_an_agent_says_is_published_where_it_said_it(tmp_path: Path) -> None:
    """A line written before a tool call is published before that call.

    An agent narrates as it works -- a sentence, a tool call, the next
    sentence -- and a reader following along needs the sentence that explains a
    call to arrive before it. Holding every word until the turn ends would put
    the whole narration after all of the work it describes, which reads as an
    agent that did a pile of things silently and then summarized them.
    """

    async def scenario() -> list[RuntimeEvent]:
        async with runtime_over(tmp_path, registry(tmp_path, narrates=True)) as (
            runtime,
            log,
        ):
            run = await runtime.start(GRAPH, {})
            return await until(log, run.run_id, "run.finished")

    events = asyncio.run(scenario())

    assert activity(events, IMPLEMENTATION) == [
        ("transcript", PROMPT),
        ("transcript", NARRATION),
        ("tool.call", NARRATED_TOOL),
        ("tool.result", NARRATED_TOOL),
        ("transcript", DONE),
    ]


def test_a_line_explaining_a_request_is_published_before_the_wait(
    tmp_path: Path,
) -> None:
    """And a permission request is where that matters most.

    It is the one point where a turn stops for as long as a person takes to
    answer, and the sentence saying why the agent is asking is written just
    before it. Held to the end of the turn, that sentence would be published
    only once somebody had answered -- so for the whole time the run was
    genuinely waiting on them, the conversation would be empty.
    """

    async def scenario() -> list[RuntimeEvent]:
        async with runtime_over(
            tmp_path, registry(tmp_path, asks=True, narrates=True)
        ) as (runtime, log):
            run = await runtime.start(GRAPH, {})
            # Everything published up to the question and no further. The
            # answer is given only after this, so whatever is in here arrived
            # while the run was still blocked on a person.
            asked = await until(log, run.run_id, "approval.requested")
            await runtime.decide(
                run.run_id,
                asked[-1].payload["approvalId"],  # type: ignore[arg-type]
                ApprovalDecision.ACCEPT,
            )
            await until(log, run.run_id, "run.finished")
            return asked

    waiting = asyncio.run(scenario())

    assert activity(waiting, IMPLEMENTATION) == [
        ("transcript", PROMPT),
        ("transcript", ASK_NARRATION),
        ("approval.requested", "run the tests"),
    ]


# --- steering ---------------------------------------------------------------


def test_steering_interrupts_the_turn_in_flight(tmp_path: Path) -> None:
    async def scenario() -> list[RuntimeEvent]:
        async with runtime_over(tmp_path, registry(tmp_path, waits_for_cancel=True)) as (
            runtime,
            log,
        ):
            run = await runtime.start(GRAPH, {})
            agent_log = tmp_path / "agent.log"
            async with asyncio.timeout(PATIENCE):
                while not agent_log.exists() or not prompts(tmp_path):
                    await asyncio.sleep(0.01)
            await runtime.steer(run.run_id, "Use the fast suite.")
            return await until(log, run.run_id, "run.finished")

    events = asyncio.run(scenario())

    assert transcript(events) == [
        ("user", PROMPT),
        ("user", "Use the fast suite."),
        ("assistant", DONE),
    ]
    assert prompts(tmp_path) == [PROMPT, "Use the fast suite."]
    assert len(sent(tmp_path, "session/new")) == 1
    assert len(sent(tmp_path, "session/cancel")) == 1


def test_steering_an_acp_execution_continues_the_same_session(tmp_path: Path) -> None:
    """The requirement, stated as what must *not* have happened.

    A message for an agent that is already running is not a question about what
    the graph should run next. So: no second `session/new`, no second entry into
    the node, and the instruction delivered as a further turn of the
    conversation the agent was already in.
    """

    async def scenario() -> dict[str, Any]:
        async with runtime_over(tmp_path, registry(tmp_path, asks=True)) as (
            runtime,
            log,
        ):
            run = await runtime.start(GRAPH, {})
            asked = await until(log, run.run_id, "approval.requested")
            approval = asked[-1]
            # Steered while the agent is blocked on a person. A runtime that had
            # suspended the graph node to ask would have nothing to deliver to.
            waiting = await runtime.snapshot(run.run_id)
            await runtime.steer(run.run_id, "Use the fast suite.")
            await runtime.decide(
                run.run_id,
                approval.payload["approvalId"],  # type: ignore[arg-type]
                ApprovalDecision.ACCEPT,
            )
            events = await until(log, run.run_id, "run.finished")
            return {
                "waiting": waiting,
                "events": events,
                "entered": runtime.entered(IMPLEMENTATION),
            }

    outcome = asyncio.run(scenario())

    assert outcome["waiting"].status.value == "awaiting_approval"
    assert [one.node_id for one in outcome["waiting"].active_executions] == [
        IMPLEMENTATION
    ]
    assert outcome["entered"] == 1
    assert transcript(outcome["events"]) == [
        ("user", PROMPT),
        ("assistant", DONE),
        ("user", "Use the fast suite."),
        ("assistant", DONE),
    ]
    # One conversation, two turns in it: the instruction reached the agent that
    # was already running rather than starting a second one.
    assert len(sent(tmp_path, "session/new")) == 1
    assert prompts(tmp_path) == [PROMPT, "Use the fast suite."]
    assert list(sessions(tmp_path).values())[0]["turns"] == [
        PROMPT,
        "Use the fast suite.",
    ]


def test_steering_sent_during_a_steered_turn_still_reaches_the_agent(
    tmp_path: Path,
) -> None:
    """The second message, said while the agent is answering the first.

    A message is delivered when the turn in flight ends, so the window a person
    types into is whichever turn that is -- and once one instruction has landed,
    the turn in flight *is* a steered one. It is also the turn they are most
    likely to be watching, because they just redirected the agent and are
    reading what it does about it.

    Draining the queue once would take it as it looked before that reply began
    and leave everything said during it on an execution the node is about to
    release. Nothing reports that: the run finishes normally, and the message
    stays on screen as a turn the agent never answered.
    """

    async def scenario() -> int:
        async with runtime_over(tmp_path, registry(tmp_path, asks_every=True)) as (
            runtime,
            log,
        ):
            run = await runtime.start(GRAPH, {})
            unsaid = ["Use the fast suite.", "And skip the linter."]
            async with asyncio.timeout(PATIENCE):
                async for event in log.stream(run.run_id):
                    if event.kind.value == "approval.requested":
                        # Said while the agent is blocked on its own question,
                        # which is the moment a person reliably has: the first
                        # into the turn the node was given, the second into the
                        # turn that is answering the first.
                        if unsaid:
                            await runtime.steer(run.run_id, unsaid.pop(0))
                        await runtime.decide(
                            run.run_id,
                            event.payload["approvalId"],  # type: ignore[arg-type]
                            ApprovalDecision.ACCEPT,
                        )
                    elif event.kind.value in ("run.finished", "run.failed"):
                        break
            return runtime.entered(IMPLEMENTATION)

    entered = asyncio.run(scenario())

    assert prompts(tmp_path) == [PROMPT, "Use the fast suite.", "And skip the linter."]
    # Both of them further turns of the one conversation, and the node entered
    # once: nothing here restarted anything to carry a message.
    assert len(sent(tmp_path, "session/new")) == 1
    assert entered == 1
    assert list(sessions(tmp_path).values())[0]["turns"] == [
        PROMPT,
        "Use the fast suite.",
        "And skip the linter.",
    ]


# --- approvals, answered by the process that raised them --------------------


def test_an_acp_permission_request_becomes_an_answerable_approval(
    tmp_path: Path,
) -> None:
    async def scenario() -> dict[str, Any]:
        async with runtime_over(tmp_path, registry(tmp_path, asks=True)) as (
            runtime,
            log,
        ):
            run = await runtime.start(GRAPH, {})
            await until(log, run.run_id, "approval.requested")
            paused = await runtime.snapshot(run.run_id)
            approval = paused.pending_approvals[0]
            stored = await runtime.store.approval(approval.approval_id)
            released = await runtime.decide(
                run.run_id, approval.approval_id, ApprovalDecision.ACCEPT
            )
            events = await until(log, run.run_id, "run.finished")
            return {
                "paused": paused,
                "approval": approval,
                "stored": stored,
                "released": released,
                "events": events,
            }

    outcome = asyncio.run(scenario())
    approval = outcome["approval"]
    stored = outcome["stored"]

    # The question, as the agent described it and a person would read it.
    assert approval.node_id == IMPLEMENTATION
    assert approval.reason == "run the tests"
    assert approval.command == "pytest"
    assert approval.execution_id in {
        one.execution_id for one in outcome["paused"].active_executions
    }
    # And, beside it, everything needed to reach the conversation again --
    # written down before the wait, not after it.
    assert stored.continuation is not None
    assert stored.continuation.agent == AGENT
    assert stored.continuation.session_id in sessions(tmp_path)
    assert stored.continuation.thread_id == str(outcome["paused"].run_id)
    assert outcome["released"].pending_approvals == ()
    assert transcript(outcome["events"]) == [("user", PROMPT), ("assistant", DONE)]
    # The call the question was about, in the agent's own ids, so a client can
    # draw the question beside the command rather than beside the whole turn.
    requested = next(
        event
        for event in outcome["events"]
        if event.kind.value == "approval.requested"
    )
    assert requested.payload["toolCallId"] == "call_1"


def test_refusing_an_acp_approval_stops_the_run_where_it_asked(
    tmp_path: Path,
) -> None:
    async def scenario() -> dict[str, Any]:
        async with runtime_over(tmp_path, registry(tmp_path, asks=True)) as (
            runtime,
            log,
        ):
            run = await runtime.start(GRAPH, {})
            asked = await until(log, run.run_id, "approval.requested")
            approval = asked[-1].payload["approvalId"]
            refused = await runtime.decide(
                run.run_id, approval, ApprovalDecision.CANCEL  # type: ignore[arg-type]
            )
            events = await until(log, run.run_id, "run.failed")
            return {"refused": refused, "events": events}

    outcome = asyncio.run(scenario())
    refused = outcome["refused"]

    assert refused.status.value == "failed"
    assert refused.error == "run the tests was not allowed"
    assert refused.active_executions == ()
    # The position is untouched: the superstep never committed, so the run can
    # be sent back and tried again.
    assert refused.next_nodes == (IMPLEMENTATION,)
    failed = [event for event in outcome["events"] if event.kind.value == "run.failed"]
    assert failed[-1].node_id == IMPLEMENTATION


def test_two_agents_asking_at_once_are_answered_separately(tmp_path: Path) -> None:
    """The reason an approval carries an execution id rather than a node name."""

    async def scenario() -> dict[str, Any]:
        async with runtime_over(tmp_path, registry(tmp_path, asks=True), pool) as (
            runtime,
            log,
        ):
            run = await runtime.start(GRAPH, {})
            await until(log, run.run_id, "approval.requested", 2)
            waiting = await runtime.snapshot(run.run_id)
            first, second = waiting.pending_approvals
            after_one = await runtime.decide(
                run.run_id, first.approval_id, ApprovalDecision.ACCEPT
            )
            await runtime.decide(
                run.run_id, second.approval_id, ApprovalDecision.ACCEPT
            )
            events = await until(log, run.run_id, "run.finished")
            return {
                "waiting": waiting,
                "first": first,
                "second": second,
                "after_one": after_one,
                "events": events,
            }

    outcome = asyncio.run(scenario())
    first, second = outcome["first"], outcome["second"]

    assert {first.node_id, second.node_id} == {NodeId("agent-1"), NodeId("agent-2")}
    assert first.execution_id != second.execution_id
    assert first.approval_id != second.approval_id
    # Answering one released one. The other agent is still waiting.
    assert [one.approval_id for one in outcome["after_one"].pending_approvals] == [
        second.approval_id
    ]
    resolved = [
        event
        for event in outcome["events"]
        if event.kind.value == "approval.resolved"
    ]
    assert [event.execution_id for event in resolved[:2]] == [
        first.execution_id,
        second.execution_id,
    ]
    # Two agents, two conversations, neither answered on the other's behalf.
    assert len(sessions(tmp_path)) == 2
    assert all(session["granted"] for session in sessions(tmp_path).values())


# --- approvals answered by a process that never raised them -----------------


def test_an_approval_survives_the_runtime_that_raised_it(tmp_path: Path) -> None:
    """The whole point of the handoff, tested by throwing the process away.

    The first runtime raises the request and is then destroyed -- driver task,
    ACP connection, agent process and all. The second is built from nothing but
    the files: a new checkpointer connection, a new store, and a graph it has
    never run. It answers the request, and the agent picks up the conversation
    it was already in.

    What must be true afterwards is the part that makes this different from a
    pause and a resume: one `session/new` across both processes, one delivery of
    the original prompt, and a `session/load` in between. Any of those going the
    other way would mean the agent had been handed its own work again as a fresh
    task.
    """

    async def raise_it() -> tuple[RunId, str]:
        async with runtime_over(
            tmp_path,
            registry(tmp_path, asks=True),
            pipeline_with_workflow_mcp,
        ) as (runtime, log):
            run = await runtime.start(GRAPH, {})
            asked = await until(log, run.run_id, "approval.requested")
            return run.run_id, str(asked[-1].payload["approvalId"])

    async def answer_it(run_id: RunId, approval_id: str) -> dict[str, Any]:
        async with runtime_over(
            tmp_path,
            registry(tmp_path, asks=True),
            pipeline_with_workflow_mcp,
        ) as (runtime, log):
            found = await runtime.snapshot(run_id)
            released = await runtime.decide(
                run_id, approval_id, ApprovalDecision.ACCEPT  # type: ignore[arg-type]
            )
            events = await until(log, run_id, "run.finished")
            return {
                "found": found,
                "released": released,
                "events": events,
                "final": await runtime.snapshot(run_id),
            }

    run_id, approval_id = asyncio.run(raise_it())
    # Nothing is alive between these two lines. Whatever the second runtime
    # knows, it read off the disk.
    outcome = asyncio.run(answer_it(run_id, approval_id))

    found = outcome["found"]
    assert found is not None
    assert found.status.value == "awaiting_approval"
    assert [one.approval_id for one in found.pending_approvals] == [approval_id]
    # Nothing was executing: the task that asked died with its process, which is
    # exactly the state the handoff exists to be answerable from.
    assert found.active_executions == ()
    assert found.next_nodes == (IMPLEMENTATION,)

    assert outcome["final"].status.value == "completed"
    assert outcome["final"].values[str(IMPLEMENTATION)] == DONE
    assert transcript(outcome["events"])[-1] == ("assistant", DONE)

    # One conversation, across two processes.
    assert len(sent(tmp_path, "session/new")) == 1
    assert len(sent(tmp_path, "session/load")) == 1
    assert sent(tmp_path, "session/new")[0]["params"]["mcpServers"] == [
        WORKFLOW_MCP_SERVER
    ]
    assert sent(tmp_path, "session/load")[0]["params"]["mcpServers"] == [
        WORKFLOW_MCP_SERVER
    ]
    session = list(sessions(tmp_path).values())[0]
    assert session["loads"] == 1
    assert session["granted"] is True
    # The original prompt was delivered once. The second turn is the
    # continuation, which says only that the question was answered.
    assert prompts(tmp_path).count(PROMPT) == 1
    assert len(prompts(tmp_path)) == 2
    assert PROMPT not in prompts(tmp_path)[1]
    # And it is not published. `role="user"` is the channel a person's own words
    # arrive on, and the continuation is the runtime telling a resumed session
    # that its question was answered -- said under that role it would read, to
    # whoever just answered the question, as something they had typed.
    assert transcript(outcome["events"]) == [("assistant", DONE)]


def test_two_lost_approvals_are_both_answered_before_anything_restarts(
    tmp_path: Path,
) -> None:
    """A superstep is plural, so a lost process can leave several mid-question.

    Restarting on the first answer would re-enter every node of the superstep,
    including the one still waiting: it has no decision to apply, so it would
    open a second conversation and put its original prompt to it as fresh work,
    and the answer sent afterwards would arrive for an execution that no longer
    exists. So the first decision is durable and inert, and the restart happens
    once, when nothing is left unanswered.
    """

    async def raise_them() -> tuple[RunId, list[str]]:
        async with runtime_over(tmp_path, registry(tmp_path, asks=True), pool) as (
            runtime,
            log,
        ):
            run = await runtime.start(GRAPH, {})
            await until(log, run.run_id, "approval.requested", 2)
            waiting = await runtime.snapshot(run.run_id)
            return run.run_id, [
                str(one.approval_id) for one in waiting.pending_approvals
            ]

    async def answer_them(run_id: RunId, approvals: list[str]) -> dict[str, Any]:
        async with runtime_over(tmp_path, registry(tmp_path, asks=True), pool) as (
            runtime,
            log,
        ):
            first, second = approvals
            after_one = await runtime.decide(
                run_id, first, ApprovalDecision.ACCEPT  # type: ignore[arg-type]
            )
            # Nothing may be moving yet. A driver started here would be running
            # the agent whose question is still on somebody's screen.
            await asyncio.sleep(0.2)
            resting = {
                "snapshot": await runtime.snapshot(run_id),
                "running": list(runtime.running()),
                "sessions": len(sent(tmp_path, "session/new")),
            }
            await runtime.decide(
                run_id, second, ApprovalDecision.ACCEPT  # type: ignore[arg-type]
            )
            await until(log, run_id, "run.finished")
            return {
                "after_one": after_one,
                "resting": resting,
                "final": await runtime.snapshot(run_id),
            }

    run_id, approvals = asyncio.run(raise_them())
    assert len(approvals) == 2
    outcome = asyncio.run(answer_them(run_id, approvals))

    # One answered, one outstanding, and the run still going nowhere.
    assert [str(one.approval_id) for one in outcome["after_one"].pending_approvals] == [
        approvals[1]
    ]
    assert outcome["resting"]["running"] == []
    assert outcome["resting"]["snapshot"].status.value == "awaiting_approval"
    assert outcome["resting"]["sessions"] == 2

    assert outcome["final"].status.value == "completed"
    # Two conversations, started once each in the first process and reloaded
    # once each in the second. A third `session/new` would be an agent handed
    # its own outstanding work as a new task.
    assert len(sent(tmp_path, "session/new")) == 2
    assert len(sent(tmp_path, "session/load")) == 2
    assert len(sessions(tmp_path)) == 2
    assert all(session["granted"] for session in sessions(tmp_path).values())
    assert all(session["loads"] == 1 for session in sessions(tmp_path).values())
    for node_id in ("agent-1", "agent-2"):
        assert prompts(tmp_path).count(f"{PROMPT} ({node_id})") == 1


def test_a_reconstructed_runtime_refuses_an_approval_it_already_answered(
    tmp_path: Path,
) -> None:
    """Answering twice is a race that lost, not a request that never existed."""

    async def raise_it() -> tuple[RunId, str]:
        async with runtime_over(tmp_path, registry(tmp_path, asks=True)) as (
            runtime,
            log,
        ):
            run = await runtime.start(GRAPH, {})
            asked = await until(log, run.run_id, "approval.requested")
            return run.run_id, str(asked[-1].payload["approvalId"])

    async def answer_twice(run_id: RunId, approval_id: str) -> str:
        async with runtime_over(tmp_path, registry(tmp_path, asks=True)) as (
            runtime,
            log,
        ):
            await runtime.decide(
                run_id, approval_id, ApprovalDecision.ACCEPT  # type: ignore[arg-type]
            )
            await until(log, run_id, "run.finished")
            with pytest.raises(Exception) as refused:
                await runtime.decide(
                    run_id, approval_id, ApprovalDecision.ACCEPT  # type: ignore[arg-type]
                )
            return type(refused.value).__name__

    run_id, approval_id = asyncio.run(raise_it())
    assert asyncio.run(answer_twice(run_id, approval_id)) == "ApprovalNotPendingError"


def test_an_answered_approval_does_not_follow_the_node_into_its_next_entry(
    tmp_path: Path,
) -> None:
    """A continuation names a question only while there is one outstanding.

    An approval answered without the process ever going away is not something
    to come back to, and leaving it on the stored continuation is worse than
    useless. The next entry into the node -- a fork here, but a loop would do --
    would find a settled decision waiting, resume a conversation nobody is
    holding, send the continuation prompt in place of its own, and auto-answer
    the first permission request it met with an answer given to a different
    question entirely.
    """

    async def scenario() -> dict[str, Any]:
        async with runtime_over(tmp_path, registry(tmp_path, asks=True)) as (
            runtime,
            log,
        ):
            run = await runtime.start(GRAPH, {})
            asked = await until(log, run.run_id, "approval.requested")
            first = str(asked[-1].payload["approvalId"])
            await runtime.decide(
                run.run_id, first, ApprovalDecision.ACCEPT  # type: ignore[arg-type]
            )
            done = await until(log, run.run_id, "run.finished")
            binding = await runtime.store.session(run.run_id, str(IMPLEMENTATION))
            history = await runtime.history(run.run_id)
            await runtime.resume_from(run.run_id, history[0].checkpoint_id)
            # The re-attempt has to ask again. Being answered without asking is
            # exactly the failure this is here for.
            again = await until(
                log, run.run_id, "approval.requested", cursor=done[-1].sequence
            )
            second = str(again[-1].payload["approvalId"])
            await runtime.decide(
                run.run_id, second, ApprovalDecision.ACCEPT  # type: ignore[arg-type]
            )
            await until(log, run.run_id, "run.finished", cursor=again[-1].sequence)
            return {"binding": binding, "first": first, "second": second}

    outcome = asyncio.run(scenario())

    assert APPROVAL_ID not in outcome["binding"].metadata
    assert outcome["second"] != outcome["first"]
    # A fresh conversation for the re-attempt, given the node's own prompt. The
    # abandoned one is not reloaded: replaying an attempt is a different
    # attempt, and continuing the old session would hand the agent its own
    # discarded work as context.
    assert len(sent(tmp_path, "session/new")) == 2
    assert sent(tmp_path, "session/load") == []
    assert prompts(tmp_path) == [PROMPT, PROMPT]


# --- durability of position, not just of the question -----------------------


def test_history_and_forks_survive_a_new_runtime(tmp_path: Path) -> None:
    """A checkpoint is LangGraph's, so it outlives whoever was driving it."""

    async def first() -> RunId:
        async with runtime_over(tmp_path, registry(tmp_path)) as (runtime, log):
            run = await runtime.start(GRAPH, {})
            await until(log, run.run_id, "run.finished")
            return run.run_id

    async def second(run_id: RunId) -> dict[str, Any]:
        async with runtime_over(tmp_path, registry(tmp_path)) as (runtime, log):
            before = await runtime.history(run_id)
            forked = await runtime.resume_from(run_id, before[0].checkpoint_id)
            await until(log, run_id, "run.finished")
            return {
                "before": before,
                "forked": forked,
                "after": await runtime.history(run_id),
                "entered": runtime.entered(IMPLEMENTATION),
            }

    run_id = asyncio.run(first())
    outcome = asyncio.run(second(run_id))
    before, after = outcome["before"], outcome["after"]

    assert [point.source for point in before] == ["start", "superstep", "superstep"]
    assert after[: len(before)] == before
    fork = after[len(before)]
    assert fork.source == "fork"
    assert fork.parent_id == before[0].checkpoint_id
    assert fork.checkpoint_id == outcome["forked"].checkpoint_id
    # A fork re-attempts the superstep, so the node ran again -- in this process,
    # which had never run it before.
    assert outcome["entered"] == 1
    # And a fresh conversation, because a replayed attempt is a different
    # attempt: continuing the abandoned one would hand the agent its own
    # discarded work as context.
    assert len(sent(tmp_path, "session/new")) == 2


# --- shutdown ---------------------------------------------------------------


def test_shutdown_leaves_no_task_and_no_agent_behind(tmp_path: Path) -> None:
    """A leaked task looks exactly like a node that is thinking. So: count them.

    The agent is left mid-request on purpose -- blocked on a permission nobody
    is going to answer -- because that is the shutdown that goes wrong: a
    subprocess holding a pipe open and a coroutine waiting on a future.
    """

    async def scenario() -> tuple[int, int, list[RunId]]:
        before = len(asyncio.all_tasks())
        agents = registry(tmp_path, asks=True)
        store = SqliteGraphRuntimeStore(tmp_path / "runtime.db")
        async with AsyncSqliteSaver.from_conn_string(
            str(tmp_path / "checkpoints.db")
        ) as saver:
            runtime = LangGraphRuntime(pipeline(saver, agents, tmp_path), store=store)
            log = EventLog()
            runtime.observe(log.append)
            run = await runtime.start(GRAPH, {})
            await until(log, run.run_id, "approval.requested")
            await runtime.aclose()
            still_running = list(runtime.running())
        store.close()
        # Two turns of the loop for the cancelled tasks to finish unwinding;
        # cancellation is delivered, not applied, at the moment it is requested.
        for _ in range(5):
            await asyncio.sleep(0)
        return before, len(asyncio.all_tasks()), still_running

    before, after, still_running = asyncio.run(scenario())

    assert still_running == []
    assert after <= before


# --- where the agent works --------------------------------------------------


def working_in(
    saver: Any, agents: ACPAgentRegistry, _where: Path
) -> LangGraphDefinition:
    """One agent node, told to work wherever the run's state says."""
    builder: StateGraph = StateGraph(State)
    builder.add_node(
        str(IMPLEMENTATION),
        ACPNode(
            agent=AGENT,
            prompt=PROMPT,
            registry=agents,
            cwd=lambda state: str(state.get("workspace") or ""),
        ),
    )
    builder.add_edge(START, str(IMPLEMENTATION))
    builder.add_edge(str(IMPLEMENTATION), END)
    return LangGraphDefinition(
        graph_id=GRAPH, name="ACP cwd", graph=builder.compile(checkpointer=saver)
    )


def test_a_node_works_in_the_directory_its_run_was_given(tmp_path: Path) -> None:
    """The checkout is the run's, so one graph has to serve every run.

    A `cwd` fixed when the graph was written would mean a definition per
    checkout -- or every run of every graph sharing one working tree, which is
    the same bug with fewer objects. Read off the state the run was started
    with, and asserted where it actually lands: the `session/new` the agent was
    sent.
    """
    checkout = tmp_path / "checkout"
    checkout.mkdir()

    async def scenario() -> None:
        async with runtime_over(tmp_path, registry(tmp_path), working_in) as (
            runtime,
            log,
        ):
            run = await runtime.start(GRAPH, {"workspace": str(checkout)})
            await until(log, run.run_id, "run.finished")

    asyncio.run(scenario())

    opened = sent(tmp_path, "session/new")
    assert [message["params"]["cwd"] for message in opened] == [str(checkout)]


def test_a_node_that_resolves_no_directory_starts_no_agent(tmp_path: Path) -> None:
    """The failure that must never be quiet: a run with nowhere to work.

    Starting the same graph without the state its resolver reads is not exotic
    -- a `WorkspaceNode` omitted or ordered after the agent, a provider that
    answered with an empty path, a fork re-entering this node from a position
    taken before anything was provisioned -- and ACP resolves an absent working
    directory against the *client's* process. So the quiet outcome here is an
    agent editing the server's own checkout, with nothing in the run saying so.

    Asserted against the agent's own log, which does not exist: the stub never
    ran, so no session was opened in the server's directory or in any other. A
    weaker check -- that the `cwd` sent was not the server's -- would pass for a
    run that started an agent somewhere else nobody chose.
    """

    async def scenario() -> Any:
        async with runtime_over(tmp_path, registry(tmp_path), working_in) as (
            runtime,
            log,
        ):
            run = await runtime.start(GRAPH, {})
            await until(log, run.run_id, "run.failed")
            return await runtime.snapshot(run.run_id)

    failed = asyncio.run(scenario())

    assert failed.status.value == "failed"
    assert "no working directory" in failed.error
    assert not (tmp_path / "agent.log").exists()


@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("large_tools", [False, True])
def test_finished_node_steering_replays_durable_transcript(
    tmp_path: Path, restart: bool, large_tools: bool,
) -> None:
    def build(saver: Any, agents: ACPAgentRegistry, where: Path) -> LangGraphDefinition:
        builder = StateGraph(State)
        builder.add_node(str(IMPLEMENTATION), ACPNode(
            agent=AGENT, prompt=PROMPT, registry=agents, cwd=str(where),
            graph_node_always_open=True,
        ))
        builder.add_edge(START, str(IMPLEMENTATION))
        builder.add_edge(str(IMPLEMENTATION), END)
        return LangGraphDefinition(
            graph_id=GRAPH, name="conversation", graph=builder.compile(checkpointer=saver)
        )

    async def exercise() -> None:
        agents = registry(tmp_path, narrates=True)
        async with runtime_over(tmp_path, agents, build=build) as (runtime, log):
            run = await runtime.start(GRAPH, {})
            await until(log, run.run_id, "run.finished")
            if large_tools:
                for kind in (EventKind.TOOL_CALL, EventKind.TOOL_RESULT, EventKind.TOOL_CALL):
                    runtime.store.append_event(RuntimeEvent(
                        run_id=run.run_id, node_id=IMPLEMENTATION, kind=kind,
                        payload={"name": "Editing files", "content": "diff" * 180_000},
                    ))
            before = runtime.store.events_since(run.run_id)
            if not restart:
                await runtime.steer(run.run_id, "Please adjust the implementation.", node_id=IMPLEMENTATION)
                await until(log, run.run_id, "run.finished", count=2)
        if restart:
            async with runtime_over(tmp_path, agents, build=build) as (runtime, log):
                restored = EventLog(runtime.store)
                assert restored.since(run.run_id) == before
                await runtime.steer(run.run_id, "Please adjust the implementation.", node_id=IMPLEMENTATION)
                await until(log, run.run_id, "run.finished")
        replay = prompts(tmp_path)[-1]
        assert PROMPT in replay
        assert DONE in replay
        assert NARRATION in replay
        assert "tool.call:" in replay
        assert "tool.result:" in replay
        assert replay.endswith("User: Please adjust the implementation.")
        assert len(replay) < 1_048_576
        if large_tools:
            assert "[Content truncated for replay.]" in replay
        assert len(prompts(tmp_path)) == 2

    asyncio.run(exercise())


@pytest.mark.parametrize("kind", [EventKind.TRANSCRIPT, EventKind.TOOL_CALL])
def test_replay_history_bounds_total_size_and_keeps_recent_context(kind: EventKind) -> None:
    history = [
        RuntimeEvent(
            run_id=RunId("replay"), kind=kind,
            payload={"text": f"message {i}: " + "x" * 20_000},
        )
        for i in range(100)
    ]
    history.append(RuntimeEvent(
        run_id=RunId("replay"), kind=EventKind.TRANSCRIPT,
        payload={"role": "assistant", "text": "Latest answer."},
    ))
    replay = _replay_history(history)
    assert len(replay) <= 512_000
    assert replay.startswith("[Earlier conversation omitted from replay.]")
    assert "message 0:" not in replay
    assert "message 99:" in replay
    assert replay.endswith("assistant: Latest answer.")
    assert len(history[0].payload["text"]) > 20_000


def test_replay_prompt_reserves_space_for_large_follow_up() -> None:
    original = "Earlier answer: " + "x" * 600_000 + " end of answer"
    history = [RuntimeEvent(
        run_id=RunId("replay"), kind=EventKind.TRANSCRIPT,
        payload={"role": "assistant", "text": original},
    )]
    opening = "Please update: " + "y" * 600_000

    replay = _replay_prompt(history, opening)

    assert len(replay) <= 1_048_576
    assert "[Content truncated for replay.]" in replay
    assert "Earlier answer:" in replay
    assert "end of answer" in replay
    assert replay.endswith(f"User: {opening}")
    assert history[0].payload["text"] == original


def test_replay_prompt_can_omit_all_history_at_input_boundary() -> None:
    history = [RuntimeEvent(
        run_id=RunId("replay"), kind=EventKind.TRANSCRIPT,
        payload={"text": "earlier answer"},
    )]
    # Leave exactly enough space for the framing and omission notice.
    prefix = "Continue the previous conversation below, including its tool history.\n\n"
    marker = "[Earlier conversation omitted from replay.]\n\n"
    opening = "x" * (1_048_576 - len(prefix) - len("\n\nUser: ") - len(marker))
    replay = _replay_prompt(history, opening)
    assert len(replay) == 1_048_576
    assert marker in replay
    assert replay.endswith(f"User: {opening}")
    with pytest.raises(ValueError, match="Shorten the message and retry"):
        _replay_prompt(history, opening + "x")


def test_node_runner_override_is_lazy_durable_and_used_on_next_execution(tmp_path: Path) -> None:
    from dataclasses import replace
    from unittest.mock import AsyncMock, patch

    from httpx import ASGITransport, AsyncClient
    from engine.graph_runtime.api import create_app

    async def scenario() -> None:
        agents = registry(tmp_path)
        agents.register(replace(agents.resolve(AGENT), name="alternate"))
        async with runtime_over(tmp_path, agents) as (runtime, log):
            run = await runtime.start(GRAPH, {})
            await until(log, run.run_id, "run.finished")
            point = next(p for p in await runtime.history(run.run_id) if IMPLEMENTATION in p.next_nodes)
            transport = ASGITransport(app=create_app(runtime))
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                topology = (await client.get(f"/api/graphs/{GRAPH}")).json()
                node = next(n for n in topology["nodes"] if n["nodeId"] == IMPLEMENTATION)
                assert node["runner"] == AGENT
                assert node["runners"] == ["alternate", AGENT]
                url = f"/api/runs/{run.run_id}/runner"
                with patch.object(runtime.store, "remember_run", new_callable=AsyncMock) as write:
                    unchanged = await client.patch(url, json={"node": IMPLEMENTATION, "runner": AGENT})
                    assert unchanged.json()["runnerOverrides"] == {}
                    write.assert_not_awaited()
                for body in (
                    {"node": IMPLEMENTATION, "runner": "unknown"},
                    {"node": "missing", "runner": "alternate"},
                    {"node": REVIEW, "runner": "alternate"},
                    {"node": IMPLEMENTATION, "runner": ""},
                ):
                    assert (await client.patch(url, json=body)).status_code == 400
                changed = await client.patch(url, json={"node": IMPLEMENTATION, "runner": "alternate"})
                assert changed.json()["runnerOverrides"] == {IMPLEMENTATION: "alternate"}
                assert changed.json()["values"] == unchanged.json()["values"]
                with patch.object(runtime.store, "remember_run", new_callable=AsyncMock) as write:
                    await client.patch(url, json={"node": IMPLEMENTATION, "runner": "alternate"})
                    write.assert_not_awaited()

        async with runtime_over(tmp_path, agents) as (runtime, log):
            assert (await runtime.snapshot(run.run_id)).runner_overrides == {IMPLEMENTATION: "alternate"}
            await runtime.resume_from(run.run_id, point.checkpoint_id)
            events = await until(log, run.run_id, "run.finished")
            started = [e for e in events if e.kind.value == "conversation.started"]
            assert started[-1].payload["agent"] == "alternate"
            assert (await runtime.store.session(run.run_id, str(IMPLEMENTATION))).agent == "alternate"
            assert runtime.topology(GRAPH).node(IMPLEMENTATION).runner == AGENT
            reset = await runtime.set_runner(run.run_id, IMPLEMENTATION, AGENT)
            assert reset.runner_overrides == {}
            other = await runtime.start(GRAPH, {})
            events = await until(log, other.run_id, "run.finished")
            assert (await runtime.snapshot(other.run_id)).runner_overrides == {}
            assert next(e for e in events if e.kind.value == "conversation.started").payload["agent"] == AGENT

    asyncio.run(scenario())


def test_runner_change_preserves_pending_approval_after_restart(tmp_path: Path) -> None:
    from dataclasses import replace

    async def scenario() -> None:
        agents = registry(tmp_path, asks=True)
        agents.register(replace(registry(tmp_path).resolve(AGENT), name="alternate"))
        async with runtime_over(tmp_path, agents) as (runtime, log):
            run = await runtime.start(GRAPH, {})
            events = await until(log, run.run_id, "approval.requested")
            approval_id = ApprovalId(str(events[-1].payload["approvalId"]))
            original = await runtime.store.session(run.run_id, str(IMPLEMENTATION))

        async with runtime_over(tmp_path, agents) as (runtime, log):
            found = await runtime.snapshot(run.run_id)
            assert found.status.value == "awaiting_approval"
            assert found.active_executions == ()
            await runtime.set_runner(run.run_id, IMPLEMENTATION, "alternate")
            await runtime.decide(run.run_id, approval_id, ApprovalDecision.ACCEPT)
            events = await until(log, run.run_id, "run.finished")
            started = next(e for e in events if e.kind.value == "conversation.started")
            assert started.payload["agent"] == AGENT
            assert started.payload["resumed"] is True
            assert started.payload["sessionId"] == original.session_id
            final = await runtime.snapshot(run.run_id)
            assert final.values[str(IMPLEMENTATION)] == DONE
            assert final.pending_approvals == ()
            assert final.runner_overrides == {IMPLEMENTATION: "alternate"}
            assert not any(e.kind.value == "approval.requested" for e in events)
            assert len(sent(tmp_path, "session/new")) == 1
            assert len(sent(tmp_path, "session/load")) == 1
            assert prompts(tmp_path).count(PROMPT) == 1
            assert len(prompts(tmp_path)) == 2
            assert list(sessions(tmp_path).values())[0]["granted"] is True
            binding = await runtime.store.session(run.run_id, str(IMPLEMENTATION))
            assert binding.agent == AGENT
            assert binding.session_id == original.session_id

            point = next(
                p for p in await runtime.history(run.run_id)
                if IMPLEMENTATION in p.next_nodes
            )
            await runtime.resume_from(run.run_id, point.checkpoint_id)
            events = await until(log, run.run_id, "run.finished", count=2)
            started = [e for e in events if e.kind.value == "conversation.started"][-1]
            assert started.payload["agent"] == "alternate"
            assert started.payload["resumed"] is False
            assert len(sent(tmp_path, "session/new")) == 2
            assert len(sent(tmp_path, "session/load")) == 1

    asyncio.run(scenario())
