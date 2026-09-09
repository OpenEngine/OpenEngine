"""An ACP agent as a LangGraph node, controllable while it runs.

    GraphRuntime.steer(execution_id)   -> NodeExecution -> ACPSession
    GraphRuntime.decide(approval_id)   -> ExecutionId   -> NodeExecution
                                                        -> ACPSession

Both arrows stop at the session. The graph node is not interrupted, suspended or
re-entered to carry either: an instruction becomes a further turn in the *same*
conversation, and an answer to a permission request resolves the future the
session's own handler is sitting on. That is why execution-level control is not
a LangGraph interrupt -- an interrupt ends the task, and the conversation the
agent was in the middle of goes with it.

## Relinquishing the process

An agent asking to run a command is not a reason to keep a worker alive. The
person may answer in a minute or on Monday, and a coroutine holding a subprocess
open in between is both expensive and fragile.

So when the agent asks, this node writes down everything needed to come back --
the `ACPContinuation` that names the conversation, and the approval id that says
which question it is waiting on -- and only then waits. If the process dies
there, nothing is lost. LangGraph's checkpoint still has the superstep
uncommitted, the store still has the question, and `decide()` in a process that
has never seen this run writes the answer down and starts the thread again:

    reload the thread -> LangGraph re-enters this node
        -> resume_continuation() -> session/load
        -> the agent asks again -> the persisted answer is applied
        -> the same conversation carries on

The agent is never handed the original prompt a second time. That distinction is
the point: replaying the prompt would be a new task that happened to look like
the old one, with the reasoning and tool history the agent had built up thrown
away. What it gets instead is `continuation_prompt`, which says only that the
outstanding request has been answered.

Reconnecting is `langgraph-acp`'s: `resume_continuation` is its call and
`ACPContinuation` is its record, stored verbatim. Nothing here re-derives what a
session id means or how a connection reloads one.

A session is resumed only when there is an answer to apply. Anywhere else --
including a fork, which re-attempts a superstep from scratch -- a fresh
conversation is the honest one: the attempt being replayed is a different
attempt, and continuing the abandoned one would hand the agent its own rejected
work as context.

## Where permission answers arrive

A `session/request_permission` arrives on the ACP connection rather than on the
graph, so it has to be routed back to the execution that owns the conversation.
The handler is `answer_permission`, and it is given to the provider:

    StdioACPProvider(name="codex", command=[...], permissions=answer_permission)

Routing is by ACP session id, which is why the table it looks in is module-level
rather than per-runtime: a session id is the agent's and unique across every
connection, while the provider holding the handler is configured before any
runtime exists.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager, AsyncExitStack
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from engine.domain import (
    ApprovalDecision,
    ApprovalId,
    ApprovalKind,
    RunFailed,
    StepCompleted,
)
from engine.ports import ApprovalHandler, ApprovalRequest
from langgraph_acp import (
    ACPAgentRegistry,
    ACPContinuation,
    ACPEventType,
    ACPPermissionOutcome,
    ACPPermissionRequest,
    ACPPrompt,
    ACPSession,
    default_registry,
    resume_continuation,
)

from engine.graph_runtime.events import EventKind
from engine.graph_runtime_langgraph.executions import NodeExecution, current_execution
from engine.runtime.step_results import (
    INVALID_COMPLETION_CORRECTIONS,
    INVALID_COMPLETION_ERROR,
)

#: Which turn each live ACP conversation belongs to, keyed by the agent's own
#: session id. Module-level because a provider is configured long before a
#: runtime is, and the key is globally unique so nothing can collide.
_TURNS: dict[str, "_Turn"] = {}

TerminalEvent = StepCompleted | RunFailed


@dataclass(frozen=True, slots=True)
class BoundMcpServer:
    """One live MCP server and the terminal result it may produce."""

    config: Mapping[str, Any]
    result: Callable[[], Awaitable[TerminalEvent]] | None = None
    clarification: Callable[[], Awaitable[None]] | None = None


class _Clarified:
    """An accepted request to pause this node without committing its state."""


_CLARIFIED = _Clarified()


McpServerBinding = Callable[
    [Mapping[str, object], NodeExecution, ApprovalHandler],
    AbstractAsyncContextManager[BoundMcpServer],
]
"""Open one invocation-bound MCP server and return its ACP description."""

_ACP_APPROVAL = "acp"
_MCP_APPROVAL = "mcp"
_REQUEST_CHANNEL = "graph_runtime.request_channel"
_REQUEST_PAYLOAD = "graph_runtime.request_payload"

#: The events that mean the agent has stopped writing and started doing, and so
#: that whatever it has said so far is a finished thought worth publishing. See
#: `ACPNode._speak`.
_INTERRUPTS_THE_NARRATION = frozenset(
    {ACPEventType.TOOL_STARTED, ACPEventType.PERMISSION_REQUESTED}
)

#: What a continuation carries for this package, under `ACPContinuation.metadata`.
#: Flat names rather than a nested object: this ends up as JSON in somebody
#: else's store, and a flat record is the one that survives being read by hand.
APPROVAL_ID = "graph_runtime.approval_id"
EXECUTION_ID = "graph_runtime.execution_id"
NODE_ID = "graph_runtime.node_id"
RUN_ID = "graph_runtime.run_id"


async def answer_permission(request: ACPPermissionRequest) -> ACPPermissionOutcome:
    """Answer `session/request_permission` for whichever execution owns it.

    Installed on the provider, and the only ACP-shaped thing on the far side of
    the generic runtime. A request naming a session nobody in this process is
    driving is declined: approving on behalf of a conversation we are not
    holding would approve something nobody was shown.
    """
    turn = _TURNS.get(request.session_id) if request.session_id is not None else None
    if turn is None:
        return ACPPermissionOutcome.cancelled()
    return await turn.ask(request)


@dataclass(slots=True)
class _Turn:
    """One node invocation's ACP state, as the permission handler needs it.

    Separate from `NodeExecution`, which is the generic contract -- two methods,
    nothing about agents. This is everything about one agent turn.
    """

    node: "ACPNode"
    execution: NodeExecution
    session_key: str
    session_id: str = ""
    answer: "_StoredAnswer | None" = None
    """An answered request from before this process existed."""
    approval_requested: bool = False
    narrating: Callable[[], Awaitable[None]] | None = None
    """`_speak`'s buffer flush, for as long as a turn is in flight.

    Called from here rather than only from the loop that fills the buffer,
    because these are two tasks: the events of a turn are consumed by `_speak`,
    while a permission request is answered on a task of the connection's own.
    Publishing the words from the task that raises the question is what puts
    them *before* it without depending on how the two get scheduled.
    """

    async def ask(self, request: ACPPermissionRequest) -> ACPPermissionOutcome:
        """Turn an ACP permission request into a runtime approval, or apply one.

        The first branch is resumption. The agent has reloaded the conversation
        and is asking the same question again, and the answer is already known;
        asking the person twice for one command is exactly what a handoff that
        had not really worked would look like.
        """
        decision = await self._approve(
            channel=_ACP_APPROVAL,
            reason=self.node.reason_for(request),
            kind=self.node.kind_of(request),
            command=self.node.command_of(request),
            tool_name=self.node.tool_of(request),
            request=dict(request.params),
            tool_call_id=self.node.call_of(request),
        )
        return _outcome(decision, request)

    async def approve(self, request: ApprovalRequest) -> ApprovalDecision:
        """Route a run-bound MCP tool's independent approval through the graph."""
        return await self._approve(
            channel=_MCP_APPROVAL,
            reason=request.reason or "run a repository tool",
            kind=request.kind,
            command=request.command or "",
            tool_name=request.tool_name or "",
            request={
                "approvalId": request.approval_id,
                "arguments": request.arguments or "",
            },
            tool_call_id=request.tool_call_id or "",
        )

    async def _approve(
        self,
        *,
        channel: str,
        reason: str,
        kind: ApprovalKind,
        command: str,
        tool_name: str,
        request: Mapping[str, object],
        tool_call_id: str,
    ) -> ApprovalDecision:
        """Ask once, leaving enough behind to reconnect after process loss."""
        self.approval_requested = True
        # Whatever the agent said on its way to asking, published before
        # anything about the question is. See `narrating`.
        if self.narrating is not None:
            await self.narrating()
        if self.answer is not None and self.answer.matches(
            channel=channel,
            kind=kind,
            command=command,
            tool_name=tool_name,
            request=request,
        ):
            decision, answered = self.answer.decision, self.answer.approval_id
            self.answer = None
            await self._settle()
            await self.execution.emit(
                EventKind.APPROVAL_RESOLVED,
                {
                    "approvalId": str(answered),
                    "decision": decision.value,
                    "resumed": True,
                },
            )
            return decision
        if self.answer is not None:
            # The reloaded conversation did not replay the request that was
            # answered. In particular, an MCP git request and an ACP permission
            # share this turn but never share authority. Refuse this unrelated
            # request without consuming the answer; the original replay may
            # still arrive later in the same turn.
            return ApprovalDecision.CANCEL
        approval_id = ApprovalId(f"approval-{uuid4().hex[:12]}")
        continuation = ACPContinuation(
            agent=self.node.agent,
            session_id=self.session_id,
            thread_id=str(self.execution.run_id),
            session_key=self.session_key,
            metadata={
                RUN_ID: str(self.execution.run_id),
                NODE_ID: str(self.execution.node_id),
                EXECUTION_ID: str(self.execution.execution_id),
                APPROVAL_ID: str(approval_id),
            },
        )
        # Bound to the run before the wait, not after: this is what a process
        # that never saw the question reads to find the conversation again, and
        # writing it afterwards would leave a window where dying loses it.
        await self.execution.runtime.store.remember_session(
            self.execution.run_id, self.session_key, continuation
        )
        decision = await self.execution.ask(
            reason=reason,
            kind=kind,
            command=command,
            tool_name=tool_name,
            session_key=self.session_key,
            continuation=continuation,
            request={
                _REQUEST_CHANNEL: channel,
                _REQUEST_PAYLOAD: dict(request),
            },
            cancel_run=channel != _MCP_APPROVAL,
            approval_id=approval_id,
            tool_call_id=tool_call_id,
        )
        # Answered without the process ever going away, so the continuation has
        # done its job and the approval comes back off it. Deliberately not in a
        # `finally`: an `ask` that does not return is a process being taken away
        # mid-question, and that is exactly when the record has to survive.
        await self._settle()
        return decision

    async def _settle(self) -> None:
        """Drop the approval from the binding, keeping the conversation.

        A continuation naming an approval means "this session is mid-question,
        and here is the question". Once it is answered that stops being true,
        and leaving it written down is worse than useless: the next entry into
        this node would find a settled decision waiting, resume a conversation
        nobody is holding, send the continuation prompt in place of the node's
        real one, and auto-answer the first permission request it met with an
        answer given to a different question entirely.
        """
        await self.execution.runtime.store.remember_session(
            self.execution.run_id,
            self.session_key,
            ACPContinuation(
                agent=self.node.agent,
                session_id=self.session_id,
                thread_id=str(self.execution.run_id),
                session_key=self.session_key,
            ),
        )


@dataclass(frozen=True, slots=True)
class _StoredAnswer:
    """A durable decision together with the exact request it answered."""

    decision: ApprovalDecision
    approval_id: ApprovalId
    channel: str
    kind: ApprovalKind
    command: str
    tool_name: str
    request: Mapping[str, object]

    def matches(
        self,
        *,
        channel: str,
        kind: ApprovalKind,
        command: str,
        tool_name: str,
        request: Mapping[str, object],
    ) -> bool:
        return (
            self.channel == channel
            and self.kind is kind
            and self.command == command
            and self.tool_name == tool_name
            and self.request == request
        )


def prompt_text(prompt: ACPPrompt) -> str:
    """The words in a prompt, for the transcript that records it being sent.

    A prompt is either a string or the content blocks ACP carries, and only the
    text of those is worth writing down: an image or a resource link is part of
    the request but not part of what a reader is reading.
    """
    if isinstance(prompt, str):
        return prompt
    return "".join(
        str(block.get("text", ""))
        for block in prompt
        if isinstance(block, Mapping) and block.get("type") == "text"
    )


def _outcome(
    decision: ApprovalDecision, request: ACPPermissionRequest
) -> ACPPermissionOutcome:
    """The agent's own option that a runtime decision means.

    ACP answers are option ids the agent offered, not a vocabulary this package
    owns, so accepting means naming one of them -- a permissive one where the
    agent classified its options, and otherwise the first, which is where every
    agent in circulation puts it. Refusing needs no option at all, which is why
    cancelling is the answer that always exists.
    """
    if decision is ApprovalDecision.CANCEL or not request.options:
        return ACPPermissionOutcome.cancelled()
    allowing = next(
        (option for option in request.options if option.kind.startswith("allow")),
        request.options[0],
    )
    return ACPPermissionOutcome.selected(allowing.option_id)


class NoWorkingDirectoryError(ValueError):
    """An agent was about to be started without being told where to work.

    Loud on purpose, because the quiet alternative is the worst outcome this
    package can produce. ACP resolves an absent working directory against the
    *client's* process -- `os.path.abspath(os.getcwd())` -- so a node that
    reached a session with nothing would get one rooted in the server's own
    checkout, and an agent with permission to edit would begin editing the
    operator's repository. Nothing in the run would say so: there is no event
    for "started somewhere unintended", and the transcript of an agent working
    in the wrong tree reads exactly like one working in the right tree.

    So no directory is never a default here and never a fallback. It is a
    refusal, at the two moments it can be caught: when a workflow is written,
    and when a run resolves one.
    """


@dataclass(frozen=True, slots=True, kw_only=True)
class ACPNode:
    """A LangGraph node that runs one ACP turn under the graph runtime.

    An async callable rather than something LangGraph has to know about, which
    is what `langgraph_acp.ACPNode` established and this keeps:

        builder.add_node(
            "implementation",
            ACPNode(agent="codex", prompt="...", cwd=checkout),
        )

    What it adds over the minimal node is everything the control surface needs:
    the session becomes the execution's, its updates become runtime events, its
    permission requests become approvals answerable by a different process, and
    steering sent while it works becomes another turn in the same conversation.
    """

    agent: str
    """The provider name to resolve: `"codex"`, `"claude"`, an application's own."""
    prompt: str | Callable[[Mapping[str, object]], ACPPrompt] = ""
    """What to say, or how to build it from the graph's state."""
    registry: ACPAgentRegistry | None = None
    """Where `agent` resolves. The shared default when omitted."""
    session_key: str = ""
    """Which conversation within the run. The node's own id when empty."""
    output_key: str = ""
    """The state key the agent's message is written to. The node id when empty."""
    kind: ApprovalKind = ApprovalKind.COMMAND_EXECUTION
    """Fallback kind for permission requests that do not require human input."""
    continuation_prompt: str = (
        "The request you were waiting on has been answered. Carry on with what "
        "you were doing; this is the same task, not a new one."
    )
    """What a resumed session is told. Never the original prompt.

    Sending that again would be a second, independent task: the agent would
    start over from the words it began with, having discarded everything it had
    worked out since. All it needs to know is that the question was answered.
    """
    graph_node_name: str = ""
    """What to call this node on screen. The node's own id when empty."""
    graph_node_kind: str = "agent"
    graph_node_description: str = ""
    graph_node_show_in_sidebar: bool = True
    """Whether clients should offer this node as a run conversation."""
    cwd: str | Callable[[Mapping[str, object]], str | None]
    """Where the session works, or how to read it off the graph's state.

    Required, and with no default, which is the one field on this node worth
    arguing about. The only default available is "wherever this process happens
    to be", and that is the server's own checkout -- see
    `NoWorkingDirectoryError`. Having none is not a state this node can be in,
    so it is not expressible: omitting it fails at the `add_node` line, under a
    type checker as well as at runtime.

    A resolver rather than only a string because the directory is usually the
    run's own: one graph serves every run, and each of them is given a checkout
    of its own by whichever node provisioned it. `components.checkout` is that
    resolver for a graph with a `WorkspaceNode` in it. Resolved per invocation,
    so the node stays a description of the work rather than a copy per checkout.
    """
    mcp_servers: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)
    """ACP MCP server descriptions made available to this node's session.

    The descriptions are passed to both ``session/new`` and ``session/load``.
    Supplying them again on load matters because a resumed conversation may be
    opened by a different agent process after the process that first hosted its
    MCP servers has gone away.
    """
    mcp_server_bindings: tuple[McpServerBinding, ...] = field(default_factory=tuple)
    """MCP servers created separately for every invocation of this node.

    A binding is entered before ``session/new`` or ``session/load`` and remains
    live until the ACP client closes. Re-entering a node after process loss
    therefore starts replacement servers while reconnecting the conversation
    the agent already owns.
    """

    def __post_init__(self) -> None:
        # A literal is checkable now; a resolver is not, and is checked on the
        # invocation that runs it. Empty rather than absent because `cwd=""` is
        # the same accident spelled differently: ACP would resolve it against
        # the process too.
        if not callable(self.cwd) and not str(self.cwd).strip():
            raise NoWorkingDirectoryError(
                f"ACPNode for agent {self.agent!r} was given an empty working "
                "directory. Name the directory the agent may work in, or pass a "
                "resolver that reads it off the run's state."
            )

    async def __call__(self, state: Mapping[str, object]) -> dict[str, object]:
        execution = current_execution()
        runtime = execution.runtime
        # First, before the store is read and before anything is launched: a
        # node that does not know where to work has nothing to resume into and
        # no session worth opening, so it fails having done nothing.
        cwd = self._cwd(state)
        key = self.session_key or str(execution.node_id)
        turn: _Turn | None = None

        async def approve(request: ApprovalRequest) -> ApprovalDecision:
            if turn is None:
                raise RuntimeError("MCP approval arrived before the ACP session opened")
            return await turn.approve(request)

        async with AsyncExitStack() as servers:
            mcp_servers = list(self.mcp_servers)
            terminal_results: list[Callable[[], Awaitable[TerminalEvent]]] = []
            clarifications: list[Callable[[], Awaitable[None]]] = []
            for binding in self.mcp_server_bindings:
                bound = await servers.enter_async_context(
                    binding(state, execution, approve)
                )
                mcp_servers.append(bound.config)
                if bound.result is not None:
                    terminal_results.append(bound.result)
                if bound.clarification is not None:
                    clarifications.append(bound.clarification)
            stored = await runtime.store.session(execution.run_id, key)
            resuming = await self._answer_to_apply(runtime, stored)
            client, session = await self._open(
                stored if resuming else None, cwd, tuple(mcp_servers)
            )
            turn = _Turn(self, execution, key, session.session_id)
            if resuming is not None:
                turn.answer = resuming
            _TURNS[session.session_id] = turn
            execution.attach(session)
            terminal_tasks = [
                asyncio.create_task(result()) for result in terminal_results
            ]
            try:
                if resuming is None:
                    await runtime.store.remember_session(
                        execution.run_id, key, self._binding(execution, session, key)
                    )
                await execution.emit(
                    EventKind.CONVERSATION_STARTED,
                    {
                        "agent": self.agent,
                        "sessionId": session.session_id,
                        "resumed": resuming is not None,
                    },
                )
                asked = self.continuation_prompt if resuming else self._prompt(state)
                # Published before the turn it starts, because a transcript that
                # holds only the agent's half is not a conversation: a reader
                # opening one has to guess what was asked, and cannot tell the work
                # the node was sent to do from the work somebody steered it into.
                #
                # The task only, never the continuation. `continuation_prompt` is
                # machinery -- it tells a resumed session that its question was
                # answered -- and `role="user"` is the channel a person's own words
                # arrive on. Publishing one as the other would put the runtime's
                # sentence on screen as something the reader appears to have typed,
                # directly beneath the approval they just answered. An empty prompt
                # is skipped for the same reason: a turn nobody spoke.
                if resuming is None and (opening := prompt_text(asked)):
                    await execution.say(opening, role="user")
                corrections = 0
                said = ""
                pending_prompts: deque[str] = deque()
                while True:
                    result = await self._speak_or_terminal(
                        turn, session, asked, terminal_tasks, clarifications
                    )
                    if isinstance(result, (StepCompleted, RunFailed)):
                        return self._terminal_update(result)
                    if result is _CLARIFIED:
                        # Do not return from the LangGraph node: that would commit
                        # this superstep and follow its outgoing edge. The live
                        # execution remains at the same graph position until a
                        # person steers the next message into this conversation.
                        corrections = 0
                        pending_prompts.extend(execution.pending_messages())
                        asked = (
                            pending_prompts.popleft()
                            if pending_prompts
                            else await execution.next_message()
                        )
                        await execution.say(asked, role="user")
                        continue

                    said = result
                    pending_prompts.extend(execution.pending_messages())
                    if pending_prompts:
                        # Steering that arrived while the agent worked is a further
                        # turn in this same conversation. Process one at a time; the
                        # next loop drains anything queued during the reply.
                        asked = pending_prompts.popleft()
                        await execution.say(asked, role="user")
                        continue
                    if not terminal_tasks:
                        return {self.output_key or str(execution.node_id): said}
                    if corrections >= INVALID_COMPLETION_CORRECTIONS:
                        raise RuntimeError(
                            f"the {execution.node_id} agent ended "
                            f"{corrections + 1} turns without reporting a valid "
                            "terminal result"
                        )
                    corrections += 1
                    asked = INVALID_COMPLETION_ERROR
                    await execution.say(asked, role="user")
            finally:
                for task in terminal_tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*terminal_tasks, return_exceptions=True)
                _TURNS.pop(session.session_id, None)
                await client.close()

    def _binding(
        self, execution: NodeExecution, session: ACPSession, key: str
    ) -> ACPContinuation:
        return ACPContinuation(
            agent=self.agent,
            session_id=session.session_id,
            thread_id=str(execution.run_id),
            session_key=key,
        )

    async def _open(
        self,
        stored: ACPContinuation | None,
        cwd: str,
        mcp_servers: tuple[Mapping[str, Any], ...],
    ) -> tuple[Any, ACPSession]:
        """Reach the conversation: the stored one when resuming, else a new one.

        `resume_continuation` is `langgraph-acp`'s, deliberately. What a session
        id means and how a connection reloads one is that package's to know; all
        this node does is keep the record and hand it back.
        """
        if stored is not None:
            return await resume_continuation(
                stored,
                registry=self.registry,
                cwd=cwd,
                mcp_servers=mcp_servers,
            )
        provider = (self.registry or default_registry()).resolve(self.agent)
        client = await provider.connect()
        try:
            return client, await client.new_session(
                cwd=cwd, mcp_servers=mcp_servers
            )
        except BaseException:
            await client.close()
            raise

    def _cwd(self, state: Mapping[str, object]) -> str:
        """Where this invocation works, refusing rather than falling back.

        The resolver is the interesting case and the reachable one: a graph
        whose `WorkspaceNode` was omitted or ordered after this node, a provider
        that answered with an empty path, or a fork re-entering this node from a
        checkpoint taken before anything had been provisioned. Each of those
        leaves the state key missing, and each of them used to mean the session
        opened in the server's own tree.
        """
        resolved = self.cwd(state) if callable(self.cwd) else self.cwd
        if not resolved or not str(resolved).strip():
            raise NoWorkingDirectoryError(
                f"ACPNode for agent {self.agent!r} resolved no working directory "
                "from this run's state. A node that provisions one -- "
                "`WorkspaceNode` -- has to run before it."
            )
        return str(resolved)

    async def _answer_to_apply(
        self, runtime: Any, stored: ACPContinuation | None
    ) -> _StoredAnswer | None:
        """The answer this node was re-entered to deliver, if it was.

        Read from the store rather than from graph state: a decision made after
        the process died was never part of any superstep, so the values a
        checkpoint holds cannot know about it.
        """
        if stored is None:
            return None
        named = stored.metadata.get(APPROVAL_ID)
        if not isinstance(named, str) or not named:
            return None
        approval_id = ApprovalId(named)
        decision = await runtime.recorded_decision(approval_id)
        record = await runtime.store.approval(approval_id)
        if decision is None or record is None:
            return None
        channel = record.request.get(_REQUEST_CHANNEL)
        request = record.request.get(_REQUEST_PAYLOAD)
        if not isinstance(channel, str) or not isinstance(request, Mapping):
            return None
        return _StoredAnswer(
            decision=decision,
            approval_id=approval_id,
            channel=channel,
            kind=record.kind,
            command=record.command,
            tool_name=record.tool_name,
            request=request,
        )

    async def _speak_or_terminal(
        self,
        turn: _Turn,
        session: ACPSession,
        prompt: ACPPrompt,
        terminal_tasks: list[asyncio.Task[TerminalEvent]],
        clarifications: list[Callable[[], Awaitable[None]]],
    ) -> str | TerminalEvent | _Clarified:
        """Wait for a turn and its accepted broker result, preferring results."""
        if not terminal_tasks and not clarifications:
            return await self._speak(turn, session, prompt)
        speaking = asyncio.create_task(self._speak(turn, session, prompt))
        clarification_tasks = [
            asyncio.create_task(clarification()) for clarification in clarifications
        ]
        try:
            done, _ = await asyncio.wait(
                (speaking, *terminal_tasks, *clarification_tasks),
                return_when=asyncio.FIRST_COMPLETED,
            )
            completed = next(
                (task for task in terminal_tasks if task in done), None
            )
            if completed is not None:
                speaking.cancel()
                await asyncio.gather(speaking, return_exceptions=True)
                return completed.result()
            if any(task in done for task in clarification_tasks):
                # A clarify call is valid only after its explanatory answer. Let
                # that turn flush, unless a terminal result supersedes it in the
                # meantime.
                done, _ = await asyncio.wait(
                    (speaking, *terminal_tasks),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                completed = next(
                    (task for task in terminal_tasks if task in done), None
                )
                if completed is not None:
                    speaking.cancel()
                    await asyncio.gather(speaking, return_exceptions=True)
                    return completed.result()
                # The broker has already accepted the clarification. A provider
                # that exits or reports cancellation while ending that turn
                # cannot revoke it.
                await asyncio.gather(speaking, return_exceptions=True)
                return _CLARIFIED
            # Give an already-accepted broker result one scheduling turn to win
            # a provider cancellation/normal-exit race.
            await asyncio.sleep(0)
            completed = next((task for task in terminal_tasks if task.done()), None)
            if completed is not None:
                return completed.result()
            try:
                return await speaking
            except Exception:
                # Some providers close their turn as soon as they submit a
                # terminal tool, just ahead of the broker accepting it. Give
                # that in-flight request the same bounded cancellation window
                # the non-graph executor does before treating the provider exit
                # as authoritative.
                if terminal_tasks:
                    done, _ = await asyncio.wait(terminal_tasks, timeout=1.0)
                    completed = next(
                        (task for task in terminal_tasks if task in done), None
                    )
                    if completed is not None:
                        return completed.result()
                raise
        finally:
            for task in clarification_tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*clarification_tasks, return_exceptions=True)

    def _terminal_update(self, event: TerminalEvent) -> dict[str, object]:
        """Turn the broker's terminal result into graph state or a run failure."""
        if isinstance(event, RunFailed):
            raise RuntimeError(event.reason)
        update: dict[str, object] = {
            self.output_key or str(current_execution().node_id): event.summary
        }
        update.update({output.name: output.value for output in event.outputs})
        return update

    async def _speak(self, turn: _Turn, session: ACPSession, prompt: ACPPrompt) -> str:
        """One ACP turn, with what happens in it republished as runtime events.

        Steering cancels a turn that is doing ordinary work so the instruction
        can become the next turn immediately. A turn that asked for permission
        is allowed to finish with its answer before queued steering is sent.

        Message deltas are gathered rather than published one by one -- a
        transcript event per token would be unreadable -- but they are gathered
        only as far as the next thing the agent does. An agent narrates what it
        is about to do and then does it, so a line written before a call is
        published before that call, and somebody following the run reads the
        explanation with the work it explains rather than after all of it.

        A permission request counts as something it does, and is the case that
        matters most: it is the one point where a turn can stop for as long as
        a person takes to answer. Held to the end of the turn, the sentence
        saying *why* the agent is asking would be published only once somebody
        had already answered -- so for the whole time the run was genuinely
        waiting on them, the conversation would have nothing in it at all.
        `langgraph-acp` streams the request before it calls the handler, for
        this reason, and `_Turn.narrating` is the other half of it.
        """
        execution = turn.execution
        said: list[str] = []
        pending: list[str] = []

        async def flush() -> None:
            # Emptied before anything is awaited, so the two tasks that can
            # call this cannot publish the same words twice.
            text = "".join(pending)
            pending.clear()
            if text:
                said.append(text)
                await execution.say(text)

        turn.narrating = flush
        turn.approval_requested = False

        async def consume() -> None:
            async for event in session.prompt(prompt):
                if event.type in _INTERRUPTS_THE_NARRATION:
                    await flush()
                await self._republish(execution, event)
                if event.type == ACPEventType.MESSAGE_DELTA:
                    block = event.data.get("content")
                    if isinstance(block, Mapping) and block.get("type") == "text":
                        text = block.get("text")
                        if isinstance(text, str):
                            pending.append(text)
            await flush()

        speaking = asyncio.create_task(consume())
        steering: asyncio.Future[None] = asyncio.create_task(
            execution.wait_for_message()
        )
        try:
            done, _ = await asyncio.wait(
                (speaking, steering), return_when=asyncio.FIRST_COMPLETED
            )
            if steering in done and not turn.approval_requested:
                speaking.cancel()
                await asyncio.gather(speaking, return_exceptions=True)
                await flush()
            else:
                await speaking
        finally:
            if not speaking.done():
                speaking.cancel()
            if not steering.done():
                steering.cancel()
            await asyncio.gather(
                speaking,
                steering,
                return_exceptions=True,
            )
            turn.narrating = None
        # The node's durable output is still the whole turn: what the graph
        # carries forward does not change with where the words were published.
        return "".join(said)

    async def _republish(self, execution: NodeExecution, event: Any) -> None:
        if event.type == ACPEventType.TOOL_STARTED:
            await execution.emit(
                EventKind.TOOL_CALL,
                {
                    "callId": str(event.data.get("toolCallId", "")),
                    "name": str(event.data.get("title") or event.data.get("kind") or ""),
                    "arguments": dict(event.data),
                },
            )
        elif event.type == ACPEventType.TOOL_UPDATED:
            await execution.emit(
                EventKind.TOOL_RESULT,
                {
                    "callId": str(event.data.get("toolCallId", "")),
                    "name": str(event.data.get("title") or ""),
                    "result": str(event.data.get("status") or "updated"),
                },
            )

    def _prompt(self, state: Mapping[str, object]) -> ACPPrompt:
        if callable(self.prompt):
            return self.prompt(state)
        return self.prompt

    # --- how one agent's permission request reads as an approval -----------

    def kind_of(self, request: ACPPermissionRequest) -> ApprovalKind:
        # ACP's activity kind is often just "other" for these tools; inspect
        # the tool's name/title before falling back to the node's fixed kind.
        for field in ("name", "toolName", "title", "kind"):
            value = request.tool_call.get(field)
            if value == "AskUserQuestion":
                return ApprovalKind.USER_INPUT
            if value in ("ExitPlanMode", "switch_mode"):
                return ApprovalKind.PLAN_APPROVAL
        return self.kind

    def reason_for(self, request: ACPPermissionRequest) -> str:
        title = request.tool_call.get("title")
        return str(title) if isinstance(title, str) and title else "run a tool"

    def command_of(self, request: ACPPermissionRequest) -> str:
        for name in ("rawInput", "input"):
            nested = request.tool_call.get(name)
            if isinstance(nested, Mapping):
                command = nested.get("command")
                if isinstance(command, str):
                    return command
        command = request.tool_call.get("command")
        return command if isinstance(command, str) else ""

    def tool_of(self, request: ACPPermissionRequest) -> str:
        for name in ("kind", "toolCallId"):
            value = request.tool_call.get(name)
            if isinstance(value, str) and value:
                return value
        return ""

    def call_of(self, request: ACPPermissionRequest) -> str:
        """The call this request is about, in the agent's own ids.

        The same id the agent puts on the `tool_call` update it sends for the
        work itself, which is what lets a reader be shown the question beside
        the command rather than beside the turn. Empty when the agent named no
        call, and a client that gets nothing here has nothing to pair.
        """
        value = request.tool_call.get("toolCallId")
        return value if isinstance(value, str) else ""


__all__ = [
    "APPROVAL_ID",
    "EXECUTION_ID",
    "NODE_ID",
    "RUN_ID",
    "ACPNode",
    "answer_permission",
    "prompt_text",
]
