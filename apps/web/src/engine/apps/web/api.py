"""HTTP surface for the assistant-ui client.

The engine owns conversations; assistant-ui owns their presentation.  This
module translates between those two vocabularies and keeps the small amount of
thread metadata that is UI-specific (title, archive status, selected runner).

Runs are streamed as newline-delimited JSON.  Their tasks are owned by the
service rather than by one response, so a refreshed browser can reconnect.
A lock per thread prevents two turns from reading the same stale transcript.

Approvals have their own replayable event feed. Their durable record is loaded
when a browser subscribes, while process-local notifications wake that feed for
later transitions without polling the transcript.
"""

from __future__ import annotations

from engine.github_concierge import Continuation, FeedbackRequest, GithubConcierge
from engine.github_concierge.github_egress import tool_permission as github_tool_permission
from engine.slack_concierge import SlackConcierge, SlackIngress
from engine.slack_concierge.slack_egress import tool_permission
from langgraph_acp.agent import ACPAgentProvider
from langgraph_acp.providers import CodexACPProvider

import asyncio
import json
import logging
import os
import time
from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
    Container,
    Iterable,
    Mapping,
    Sequence,
)
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from urllib.parse import quote, urlsplit
from uuid import uuid4

from engine.apps.web import source_control as source_control_settings
from engine.apps.web.github_activity import GithubActivityLog, activity_json
from engine.apps.web.github_ingress import GithubComment, GithubIngress
from engine.apps.web.github_login import GitHubLogin, GitHubLoginConfig
from engine.apps.web.github_auth import (
    DeviceFlowComplete,
    DeviceFlowState,
    GitHubAuthError,
    GitHubCredentialStore,
    credentials_from_device_flow,
    poll_device_flow,
    start_device_flow,
)
from engine.apps.web.gitlab_auth import (
    DeviceFlowComplete as GitLabDeviceFlowComplete,
    GitLabAuthError,
    GitLabCredentialStore,
    credentials_from_device_flow as gitlab_credentials_from_device_flow,
    normalize_origin as normalize_gitlab_origin,
    poll_device_flow as poll_gitlab_device_flow,
    start_device_flow as start_gitlab_device_flow,
)
from engine.apps.web.source_control import (
    SourceControlPreferences,
)
from engine.apps.web.utilization import (
    UtilizationService,
    utilization_json,
)
from engine.adapters.communications.slack import (
    SlackAuthError,
    SlackCommunications,
    SlackCredentialStore,
    authorization_url as slack_authorization_url,
    exchange_code as exchange_slack_code,
    revoke_token as revoke_slack_token,
    verify_signature as verify_slack_signature,
)
from engine.domain import (
    AgentId,
    AgentInstanceId,
    AgentRunId,
    AgentRunStatus,
    ApprovalDecision,
    ApprovalId,
    ApprovalRecord,
    Message,
    Milestone,
    MilestoneId,
    MilestoneScope,
    Project,
    ProjectId,
    Role,
    RunId,
    RunOrigin,
    RunPhase,
    RunState,
    ScopingPlan,
    ScopingPolicy,
    TaskId,
    WorkflowId,
    WorkspaceId,
    WorkOrder,
    WorkOrderId,
    WorkOrderSpec,
    WorkOrderStatus,
    instance_id_for_project,
    project_id_for_instance,
)
from engine.graph_runtime.inputs import resolve_inputs
from engine.graph_runtime import (
    EventKind,
    EventLog,
    GraphCompilationError,
    GraphId,
    GraphRuntime,
    GraphRuntimeError,
    GraphWorkflow,
    NodeId,
    RunSnapshot,
    RunStatus,
    RuntimeEvent,
    UnknownGraphError,
)
from engine.graph_runtime import create_app as create_graph_app
from engine.graph_runtime_langgraph.store import PullRequestRecord
from engine.ports import (
    AgentRunner,
    ApprovalHandler,
    InteractiveAgentRunner,
    Message as CommunicationsMessage,
    StateStore,
    UserInputAnswer,
    WorkspaceState,
)
from engine.runtime import (
    PLANNER,
    AgentSession,
    ApprovalBroker,
    ApprovalConfig,
    ApprovalDecisionNotAllowedError,
    ApprovalNotPendingError,
    RunNotifier,
    RunReader,
    UnknownApprovalError,
    UserInputNotAllowedError,
    WorkflowCatalog,
    WorkflowRunView,
    WorkOrdersConfig,
    load_engine_config,
    load_workflow_catalog,
)
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.types import Receive, Scope, Send


@dataclass(slots=True)
class ChatThread:
    """UI metadata for one engine agent instance."""

    instance_id: AgentInstanceId
    agent_id: AgentId
    runner: str
    title: str = "New chat"
    archived: bool = False
    workspace_root: str | None = None
    workspace_id: WorkspaceId | None = None
    workspace_ref: str | None = None
    """What to check out to read this chat's work, checkout or no checkout."""


class ActiveRun:
    """One agent turn whose lifetime is independent of an HTTP connection.

    Subscribers receive complete content snapshots, so a browser that refreshes
    can reconnect without needing to know which individual events it missed. An
    approval is the same idea and for the same reason: the turn is paused on a
    question, and a subscriber that arrives after it was asked has to be told
    the question rather than left watching a stream that has gone quiet.
    """

    def __init__(
        self,
        agent_run_id: AgentRunId,
        known_tool_call_ids: Iterable[str] = (),
    ) -> None:
        self.agent_run_id = agent_run_id
        self.content: list[dict[str, object]] = []
        # assistant-ui registers tool calls as resources by id and throws when
        # one occurs twice. Providers can replay an already completed item, so
        # keep the ids from earlier turns as well as the parts in this one.
        self._tool_calls: dict[str, dict[str, object]] = {
            call_id: {} for call_id in known_tool_call_ids
        }
        self.approvals: dict[str, dict[str, object]] = {}
        """The latest snapshot of every request this run has raised, by id.

        A map rather than "the one the turn is on", because what a subscriber
        needs is each request's *transition*: a turn let go by a decision often
        asks its next question before anyone has been told about the answer, and
        a single slot would hand the new question over in place of it -- leaving
        a card waiting forever on a request that was decided.
        """
        self._approval_transitions: list[dict[str, object]] = []
        """Every distinct state the run stream must deliver, in order.

        The latest-state map makes reconnect snapshots cheap, but cannot serve
        as an event queue: a pending request may become decided before the
        stream task next runs. Keeping the transitions separately makes stream
        delivery independent of event-loop scheduling.
        """
        self.error: str | None = None
        self.done = False
        self._revision = 0
        self._changed = asyncio.Condition()
        self._task: asyncio.Task[None] | None = None

    def start(self, say: Awaitable[str]) -> None:
        self._task = asyncio.create_task(self._run(say))

    async def cancel(self) -> None:
        if self._task is None or self._task.done():
            return
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)

    async def stream(self) -> AsyncIterator[bytes]:
        revision = 0
        transition_index = 0
        sent: dict[str, dict[str, object]] = {}
        while True:
            async with self._changed:
                await self._changed.wait_for(
                    lambda: self._revision > revision or self.done
                )
                revision = self._revision
                content = [dict(part) for part in self.content]
                transitions = [
                    dict(value)
                    for value in self._approval_transitions[transition_index:]
                ]
                transition_index += len(transitions)
                error = self.error
                done = self.done

            for approval in transitions:
                approval_id = str(approval["id"])
                # Whole snapshots, including the resolved ones: a client that
                # missed the decision would otherwise go on showing a prompt
                # for a request that has already been answered. Every one that
                # has moved rather than only the newest, because several can
                # move between two wakes and the one being answered is exactly
                # the one that would be dropped. Emitted before the terminal
                # events so the last thing said about a request is never lost to
                # the run ending in the same breath.
                if sent.get(approval_id) == approval:
                    continue
                sent[approval_id] = approval
                yield _json_line({"type": "approval", "approval": approval})
            if error is not None:
                yield _json_line({"type": "error", "error": error})
                return
            if done:
                yield _json_line({"type": "done", "content": content})
                return
            yield _json_line({"type": "content", "content": content})

    async def _run(self, say: Awaitable[str]) -> None:
        try:
            answer = await say
            if answer and not any(
                part.get("type") == "text" and part.get("text") == answer
                for part in self.content
            ):
                self.content.append({"type": "text", "text": answer})
            await self._finish()
        except Exception as error:
            self.error = f"{type(error).__name__}: {error}"
            await self._finish()
        except asyncio.CancelledError:
            await self._finish()
            raise

    async def observe(self, message: Message) -> None:
        if not _merge_message(self.content, message, self._tool_calls):
            return
        async with self._changed:
            self._revision += 1
            self._changed.notify_all()

    async def present_approval(self, approval: ApprovalRecord) -> None:
        """Publish what the turn is waiting on, and wake the subscribers.

        For a pause: nothing else is going to happen on this run until somebody
        answers, so this is the only thing that will wake anyone.
        """
        snapshot = _approval_json(approval)
        async with self._changed:
            self.approvals[str(approval.approval_id)] = snapshot
            self._approval_transitions.append(snapshot)
            self._revision += 1
            self._changed.notify_all()

    def note_approval(self, approval: ApprovalRecord) -> None:
        """Update the snapshot without waking anyone, for a run that is ending.

        Synchronous on purpose. The wake that matters is the one the run's own
        ending sends a moment later, and awaiting a lock here would yield the
        event loop back to the very turn being torn down.
        """
        snapshot = _approval_json(approval)
        self.approvals[str(approval.approval_id)] = snapshot
        self._approval_transitions.append(snapshot)

    async def _finish(self) -> None:
        async with self._changed:
            self.done = True
            self._revision += 1
            self._changed.notify_all()


class ApprovalFeed:
    """Replay durable approval snapshots, then push each later transition.

    Persistence remains the source of truth. The condition is only a wake-up
    signal, so reconnecting after a lost HTTP connection cannot lose an event.
    """

    def __init__(self, store: StateStore) -> None:
        self._store = store
        self._revisions: dict[AgentInstanceId, int] = {}
        self._changed: dict[AgentInstanceId, asyncio.Condition] = {}

    async def publish(self, approval: ApprovalRecord) -> None:
        condition = self._changed.setdefault(approval.instance_id, asyncio.Condition())
        async with condition:
            self._revisions[approval.instance_id] = (
                self._revisions.get(approval.instance_id, 0) + 1
            )
            condition.notify_all()

    async def stream(self, instance_id: AgentInstanceId) -> AsyncIterator[bytes]:
        condition = self._changed.setdefault(instance_id, asyncio.Condition())
        sent: dict[str, dict[str, object]] = {}
        # Flush the response immediately even when this conversation has never
        # asked for approval. EventSource ignores comment frames.
        yield b": connected\n\n"
        while True:
            revision = self._revisions.get(instance_id, 0)
            approvals = await self._store.list_approvals(instance_id=instance_id)
            for record in approvals:
                approval = _approval_json(record)
                approval_id = str(record.approval_id)
                if sent.get(approval_id) == approval:
                    continue
                sent[approval_id] = approval
                yield _server_event(approval)

            async with condition:
                await condition.wait_for(
                    lambda: self._revisions.get(instance_id, 0) > revision
                )


class BuiltClient(StaticFiles):
    """The Vite build, cached the way its filenames say it should be.

    Asset names carry a content hash, so those files are safe to keep forever
    and are never the reason a browser is out of date. The page that *names*
    them is the opposite: served without instructions, browsers cache it
    heuristically and go on asking for the hashed files of a build that no
    longer exists, which arrives as a blank page and a pair of 404s. So the
    entry point is revalidated every time and the hashed assets are not.
    """

    def file_response(
        self,
        full_path: os.PathLike[str],
        stat_result: os.stat_result,
        scope: Scope,
        status_code: int = 200,
    ) -> Response:
        response = super().file_response(full_path, stat_result, scope, status_code)
        immutable = Path(full_path).parent.name == "assets"
        response.headers["cache-control"] = (
            "public, max-age=31536000, immutable" if immutable else "no-cache"
        )
        return response


class ThreadService:
    """Coordinates assistant-ui threads over an ``AgentSession``."""

    def __init__(
        self,
        session: AgentSession,
        runners: Mapping[str, AgentRunner],
        approval_policy: ApprovalConfig = ApprovalConfig(),
        *,
        approval_observer: Callable[[ApprovalRecord], Awaitable[None]] | None = None,
    ) -> None:
        self.session = session
        self.approvals = ApprovalBroker(
            session.state_store, approval_policy, observe=approval_observer
        )
        """Public alongside `session`: the same durable boundary, for pauses."""
        self._runners = runners
        self._threads: dict[AgentInstanceId, ChatThread] = {}
        self._locks: dict[AgentInstanceId, asyncio.Lock] = {}
        self._active_runs: dict[AgentInstanceId, ActiveRun] = {}
        self._restored = False
        self._restore_lock = asyncio.Lock()

    async def list(self) -> tuple[ChatThread, ...]:
        await self._restore()
        return tuple(reversed(self._threads.values()))

    async def get(self, instance_id: AgentInstanceId) -> ChatThread | None:
        await self._restore()
        thread = self._threads.get(instance_id)
        if thread is not None:
            return thread
        # A conversation may be created after this web process restored its
        # initial registry. Resolve direct links from the durable store instead
        # of requiring a server restart.
        instance = await self.session.instance(instance_id)
        if instance is None:
            return None
        thread = ChatThread(
            instance.instance_id,
            instance.agent_id,
            (
                instance.runner
                if instance.runner in self.session.runners
                else self.session.default_runner
            ),
            title=instance.title,
            archived=instance.archived,
        )
        self._threads[instance.instance_id] = await self._sync_workspace(thread)
        self._locks[instance.instance_id] = asyncio.Lock()
        return thread

    async def create(self, agent_id: AgentId, runner: str) -> ChatThread:
        await self._restore()
        if runner not in self.session.runners:
            raise ValueError(f"unknown runner {runner!r}")
        instance = await self.session.start(agent_id, runner=runner)
        thread = ChatThread(instance.instance_id, agent_id, runner)
        await self._sync_workspace(thread)
        self._threads[instance.instance_id] = thread
        self._locks[instance.instance_id] = asyncio.Lock()
        return thread

    async def attach_workspace(self, instance_id: AgentInstanceId) -> ChatThread:
        """Give this chat a checkout again -- or a first one."""
        thread = await self._require_idle(instance_id)
        async with self._locks[instance_id]:
            state = await self.session.attach_workspace(instance_id)
        return self._apply_workspace_state(thread, state)

    async def detach_workspace(self, instance_id: AgentInstanceId) -> ChatThread:
        """Release this chat's checkout, keeping its work on the branch."""
        thread = await self._require_idle(instance_id)
        async with self._locks[instance_id]:
            state = await self.session.detach_workspace(instance_id)
        return self._apply_workspace_state(thread, state)

    def _apply_workspace_state(
        self, thread: ChatThread, state: WorkspaceState | None
    ) -> ChatThread:
        """Refresh every loaded conversation sharing the changed workspace."""
        workspace_id = state.workspace_id if state is not None else thread.workspace_id
        if workspace_id is not None:
            for cached in self._threads.values():
                if cached.workspace_id == workspace_id:
                    _with_workspace(cached, state)
        return _with_workspace(thread, state)

    async def _require_idle(self, instance_id: AgentInstanceId) -> ChatThread:
        """A workspace is not the agent's to lose in the middle of using it.

        The turn lock alone would serialize this correctly but leave the
        request hanging for as long as the agent runs, which reads as a broken
        button rather than a busy one.
        """
        thread = await self._require(instance_id)
        if self.active_run(instance_id) is not None:
            raise RuntimeError("this chat has a run in progress")
        return thread

    async def delete(self, instance_id: AgentInstanceId) -> None:
        await self._restore()
        self._threads.pop(instance_id, None)
        self._locks.pop(instance_id, None)

    async def history(self, instance_id: AgentInstanceId) -> tuple[Message, ...]:
        await self._require(instance_id)
        return await self.session.history(instance_id)

    async def say(
        self,
        instance_id: AgentInstanceId,
        text: str,
        runner: str | None,
        observed: asyncio.Queue[Message],
        on_approval: ApprovalHandler | None = None,
        agent_run_id: AgentRunId | None = None,
    ) -> str:
        thread = await self._require(instance_id)
        selected_runner = runner or thread.runner
        if selected_runner not in self.session.runners:
            raise ValueError(f"unknown runner {selected_runner!r}")
        thread.runner = selected_runner
        await self._persist_metadata(thread)

        async with self._locks[instance_id]:
            turn = await self.session.say(
                instance_id,
                text,
                runner=selected_runner,
                on_message=observed.put_nowait,
                on_approval=on_approval,
                agent_run_id=agent_run_id,
            )
        return turn.message.content

    async def start_run(
        self, instance_id: AgentInstanceId, text: str, runner: str | None
    ) -> ActiveRun:
        thread = await self._require(instance_id)
        await self.require_somewhere_to_run(instance_id)
        history = await self.session.history(instance_id)
        initial_message_count = len(history)
        current = self.active_run(instance_id)
        if current is not None:
            raise RuntimeError("this chat already has a run in progress")

        observed: asyncio.Queue[Message] = asyncio.Queue()
        # Named before it starts, because the approvals it raises are brokered
        # against this run and a decision has to be able to name it too.
        agent_run_id = _new_agent_run_id()
        selected_runner = runner or thread.runner
        run = ActiveRun(agent_run_id, _tool_call_ids(history))
        self._active_runs[instance_id] = run
        on_approval = None
        # The runner the session will hand this turn to, not the one the name
        # alone would pick: an agent that only reads is answered by a different
        # object, and both whether it can pause and how it reads its own
        # requests are that object's to say. An unknown name or an agent this
        # process no longer composes leaves this unresolved, and the turn then
        # fails in the session exactly where it failed before.
        profile = self.session.profiles.get(thread.agent_id)
        selected = (
            self.session.runner_for(thread.agent_id, selected_runner)
            if profile is not None and selected_runner in self.session.runners
            else None
        )
        if profile is not None and isinstance(selected, InteractiveAgentRunner):
            on_approval = self.approvals.handler(
                agent_run_id=agent_run_id,
                instance_id=instance_id,
                runner=selected_runner,
                present=run.present_approval,
                # Where this turn will actually work, so consent it collects is
                # bounded by the same worktree the agent is standing in.
                workspace_id=thread.workspace_id,
                # How this provider's requests read as Engine capabilities, so
                # the configured policy has something to evaluate them against.
                translator=selected.permission_translator,
                # And what this agent is, which the policy does not get to
                # widen: a planner asking to edit is refused here, whatever the
                # deployment allows a coder.
                read_only=profile.read_only,
            )

        async def execute() -> str:
            task = asyncio.create_task(
                self.say(
                    instance_id,
                    text,
                    selected_runner,
                    observed,
                    on_approval,
                    agent_run_id,
                )
            )
            try:
                while not task.done() or not observed.empty():
                    try:
                        async with asyncio.timeout(0.1):
                            message = await observed.get()
                    except TimeoutError:
                        continue
                    await run.observe(message)
                return await task
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                if on_approval is not None:
                    # However this turn ended, nothing is waiting on its
                    # requests any more -- a provider that died mid-question
                    # leaves one here. Every one of them, because a client is
                    # showing whichever it was last told about, and any of those
                    # has to stop saying "pending", whoever resolved it.
                    await self.approvals.interrupt_run(agent_run_id)
                    for asked in await self.session.state_store.list_approvals(
                        agent_run_id=agent_run_id
                    ):
                        run.note_approval(asked)

        run.start(execute())
        # Ensure a refresh can load the submitted question before this POST
        # starts returning streamed response bytes.
        while (
            len(await self.session.history(instance_id)) <= initial_message_count
            and not run.done
        ):
            await asyncio.sleep(0)
        return run

    def active_run(self, instance_id: AgentInstanceId) -> ActiveRun | None:
        run = self._active_runs.get(instance_id)
        return run if run is not None and not run.done else None

    def latest_run(self, instance_id: AgentInstanceId) -> ActiveRun | None:
        """The latest run, including a just-finished run needed by a racing resume."""
        return self._active_runs.get(instance_id)

    async def decide_approval(
        self,
        instance_id: AgentInstanceId,
        approval_id: ApprovalId,
        decision: str,
        agent_run_id: AgentRunId | None = None,
    ) -> ApprovalRecord:
        """Answer what this chat's current run is paused on.

        Scoped to the run rather than the conversation: an id from a turn that
        has already ended names a provider process nobody can resume, and
        applying its answer to whatever is running now would approve a command
        the user never saw.
        """
        await self._require(instance_id)
        run = self.active_run(instance_id)
        try:
            chosen = ApprovalDecision(decision)
        except ValueError:
            raise ApprovalDecisionNotAllowedError(
                f"unknown decision {decision!r}"
            ) from None
        record = await self.approvals.decide(
            approval_id,
            chosen,
            instance_id=instance_id,
            agent_run_id=run.agent_run_id if run is not None else agent_run_id,
        )
        if run is not None:
            await run.present_approval(record)
        return record

    async def answer_question(
        self,
        instance_id: AgentInstanceId,
        approval_id: ApprovalId,
        answers: tuple[UserInputAnswer, ...],
        agent_run_id: AgentRunId | None = None,
    ) -> ApprovalRecord:
        """Answer a structured prompt from this chat's current run."""

        await self._require(instance_id)
        run = self.active_run(instance_id)
        record = await self.approvals.answer(
            approval_id,
            answers,
            instance_id=instance_id,
            agent_run_id=run.agent_run_id if run is not None else agent_run_id,
        )
        if run is not None:
            await run.present_approval(record)
        return record

    async def stop_run(self, instance_id: AgentInstanceId) -> None:
        """Stop this chat's run, whether it is working or waiting on a person.

        What it was waiting on is resolved as a cancellation before the turn is
        torn down, so the answer to "was that command allowed?" is a recorded
        no rather than a row that stops mid-sentence. Tearing the turn down
        then does the rest: cancelling one request would not oblige the agent
        to stop asking, and stopping means stopping.
        """
        run = self.active_run(instance_id)
        if run is None:
            return
        for resolved in await self.approvals.cancel_run(run.agent_run_id):
            run.note_approval(resolved)
        await run.cancel()
        await self._record_cancelled(run.agent_run_id)

    async def _record_cancelled(self, agent_run_id: AgentRunId) -> None:
        """Record the stopped run as a cancellation, however the turn ended.

        A cancelled approval is a decision the provider can act on, so a
        well-behaved one answers it by tidying up and returning -- and a turn
        that returns is a turn the session records as a success. Left there,
        stopping a paused run would read afterwards as one that finished
        normally. Whatever the provider made of the last second, the user
        withdrew this turn.
        """
        store = self.session.state_store
        agent_run = await store.agent_run(agent_run_id)
        if agent_run is None or agent_run.status is AgentRunStatus.CANCELLED:
            return
        await store.record_agent_run(
            replace(agent_run, status=AgentRunStatus.CANCELLED, summary="cancelled")
        )

    async def generate_title(
        self,
        instance_id: AgentInstanceId,
        opening_text: str | None = None,
        runner: str | None = None,
    ) -> str:
        """Ask the thread's agent for a title without changing its transcript."""
        thread = await self._require(instance_id)
        project_id = project_id_for_instance(instance_id)
        project = await self.session.state_store.load_project(project_id)
        names_project = thread.title == "New project" and project is not None
        if thread.title != "New chat" and not names_project:
            return thread.title
        selected_runner = runner or thread.runner
        if selected_runner not in self.session.runners:
            raise ValueError(f"unknown runner {selected_runner!r}")
        async with self._locks[instance_id]:
            if thread.title not in {"New chat", "New project"}:
                return thread.title
            history = await self.session.history(instance_id)
            title_context = (
                (*history, Message.user(opening_text)) if opening_text else history
            )
            # The session's answer rather than the name's, so a chat with an
            # agent that only reads is named by a runner that only reads too.
            turn = await self.session.runner_for(
                thread.agent_id, selected_runner
            ).run_turn(
                _new_agent_run_id(),
                self.session.profiles[thread.agent_id],
                (*title_context, Message.user(_TITLE_PROMPT)),
                # Naming a chat reads the transcript, not the tree, so a
                # detached one is named where the process runs rather than
                # failing on a directory it does not need.
                workspace_id=thread.workspace_id if thread.workspace_root else None,
            )
        title = _clean_title(turn.message.content)
        if title:
            # Project creation is part of thread initialization, before the
            # client receives the permalink. Rename that durable placeholder
            # before consuming the sentinel title so an interrupted request is
            # always safe to retry after a reload.
            if project is not None and names_project:
                # Renamed rather than rewritten, so a project put away while it
                # was still being named comes back still put away.
                await self.session.state_store.save_project(
                    replace(project, name=title)
                )
            thread.title = title
            await self._persist_metadata(thread)
        return thread.title

    async def update_metadata(
        self,
        instance_id: AgentInstanceId,
        *,
        title: str | None = None,
        runner: str | None = None,
        archived: bool | None = None,
    ) -> ChatThread:
        thread = await self._require(instance_id)
        if runner is not None and runner not in self.session.runners:
            raise ValueError(f"unknown runner {runner!r}")
        if title is not None:
            thread.title = title
        if runner is not None:
            thread.runner = runner
        if archived is not None:
            thread.archived = archived
        await self._persist_metadata(thread)
        return thread

    async def _persist_metadata(self, thread: ChatThread) -> None:
        await self.session.update_instance_metadata(
            thread.instance_id,
            thread.title,
            thread.archived,
            thread.runner,
        )

    async def _require(self, instance_id: AgentInstanceId) -> ChatThread:
        thread = await self.get(instance_id)
        if thread is None:
            raise KeyError(f"no chat thread {instance_id!r}")
        return thread

    async def require_somewhere_to_run(self, instance_id: AgentInstanceId) -> None:
        """Refuse a turn a detached chat cannot run, in words the UI can act on.

        The runner would fail on the missing directory anyway, several layers
        down and phrased as a lookup error. A chat that never had a workspace
        is left alone: it runs where the process was told to.
        """
        try:
            workspace = await self.session.workspace(instance_id)
            detached = workspace is not None and not workspace.attached
        except KeyError:
            detached = True
        if detached:
            raise RuntimeError(
                "this chat's worktree is detached; reattach it to run the agent"
            )

    async def _sync_workspace(self, thread: ChatThread) -> ChatThread:
        """Record what the provider currently says about this chat's workspace.

        Conversations outlive their checkouts -- `git worktree remove`, a swept
        /tmp, a reboot -- so a chat is listed with whatever is left of its
        workspace rather than failing the request. A provider that disowns the
        id entirely is treated the same way: the chat is simply one without a
        workspace, and attaching offers it a new one.
        """
        try:
            return _with_workspace(
                thread, await self.session.workspace(thread.instance_id)
            )
        except KeyError:
            return _with_workspace(thread, None)

    async def _restore(self) -> None:
        """Populate the UI registry from the durable conversation store once."""
        if self._restored:
            return
        async with self._restore_lock:
            if self._restored:
                return
            instances = await self.session.instances()
            for instance in reversed(instances):
                thread = ChatThread(
                    instance.instance_id,
                    instance.agent_id,
                    (
                        instance.runner
                        if instance.runner in self.session.runners
                        else self.session.default_runner
                    ),
                    title=instance.title,
                    archived=instance.archived,
                )
                self._threads[instance.instance_id] = await self._sync_workspace(thread)
                self._locks[instance.instance_id] = asyncio.Lock()
            # A CLI subprocess does not survive the server that spawned it, so
            # a request still marked pending here was asked by a process that
            # no longer exists and can never be answered.
            await self.approvals.interrupt_orphans()
            self._restored = True


#: Where the graph runtime's sub-application is served from, so its addresses
#: are `/graph/api/runs/...` and cannot collide with this app's own `/api`.
GRAPH_PREFIX = "/graph"

#: Graph lifecycle events that change what a WorkOrder row should read.
GRAPH_EVENT_PHASES: Mapping[EventKind, RunPhase] = {
    EventKind.RUN_FORKED: RunPhase.RUNNING_AGENT,
    EventKind.RUN_FINISHED: RunPhase.SUCCEEDED,
    EventKind.RUN_FAILED: RunPhase.FAILED,
}

#: The same three answers, as the graph engine reports them when asked rather
#: than when it announces them. A run waiting on a person is still working as
#: far as a WorkOrder row is concerned: what it is waiting for is a question
#: only the graph engine's own API can show today.
GRAPH_PHASES: Mapping[RunStatus, RunPhase] = {
    RunStatus.RUNNING: RunPhase.RUNNING_AGENT,
    RunStatus.AWAITING_APPROVAL: RunPhase.RUNNING_AGENT,
    RunStatus.COMPLETED: RunPhase.SUCCEEDED,
    RunStatus.FAILED: RunPhase.FAILED,
}


def _graph_workorder_name(values: object) -> str:
    """The concise name a graph's naming node left in its state."""
    if not isinstance(values, Mapping):
        return ""
    value = values.get("name")
    if not isinstance(value, str) or not value.strip():
        return ""
    first_line = value.strip().splitlines()[0]
    return first_line.strip(" \t\"'`).:;!?")[:120]


#: Where this module says what went wrong with something nobody asked it about
#: -- a graph engine that would not open, a stranded run it could not pick back
#: up. Those go to the log rather than to a person, because the person who
#: would read them is not in the room when a server starts.
log = logging.getLogger(__name__)

#: How long the forge lookups that authorize a GitHub comment may take before
#: the comment is abandoned. The ingress behind them has one worker, so this is
#: not only that comment's latency: whatever it waits, every comment queued
#: after it waits too. Long enough to cover a slow-but-working forge, short
#: enough that a hung one costs a redelivery rather than the queue.
GITHUB_AUTHORIZATION_TIMEOUT_SECONDS = 45


@dataclass(slots=True)
class _GraphSurface:
    """The graph engine, once the server has started it.

    Both fields are empty until the application starts, because opening the
    engine means opening files and that is something a running server owns
    rather than something building one does. A request that arrives before then
    is told the graph engine is not running rather than being given half of it.

    They stay empty when the engine could not be opened for a reason outside
    the graphs themselves, which is what keeps that kind of failure to the
    graph feature: no engine, no graph entries in the dropdown, and the
    rest of the application carries on. A graph that does not *compile* never
    gets this far -- it stops the server, because it is a definition somebody
    has to fix.
    """

    runtime: GraphRuntime | None = None
    app: Starlette | None = None


class MilestoneScoping(Protocol):
    """The configured in-process scoper supplied by the composition root."""

    async def run(
        self,
        *,
        workorders: Sequence[WorkOrder],
        milestone: MilestoneScope,
        policy: ScopingPolicy,
    ) -> ScopingPlan: ...


class GithubProvenance(Protocol):
    """The half of the runtime's store that says who owns a pull request.

    Named as the pair it is used as: a comment is routed by reading the claim,
    and a work order started for a comment is findable only if it writes one.
    Stated as a protocol because this module reaches the store by duck-typing
    -- the control surface is deliberately forge-agnostic -- and a runtime that
    keeps no provenance answers ``None`` here rather than growing a method
    shaped like a pull request.
    """

    async def run_for_pull_request(self, repository: str, number: int) -> RunId | None: ...

    async def claim_pull_request(
        self, record: PullRequestRecord, *, replacing: RunId | None = None
    ) -> RunId: ...


#: The statuses a run can still be steered in. A completed or failed run has
#: no execution listening, so feedback for the pull request it opened is a new
#: request rather than a continuation of that one.
STEERABLE_RUN_STATUSES = frozenset({RunStatus.RUNNING, RunStatus.AWAITING_APPROVAL})


def _reentry_node(runtime: GraphRuntime, snapshot: RunSnapshot) -> NodeId | None:
    """Where feedback re-enters this run, or ``None`` to steer whatever runs.

    Untargeted steering reaches the execution in flight, and there is none once
    a run is parked at human review -- which is exactly when review feedback
    arrives. An always-open node is the graph's own statement of where it may
    be sent back to, so naming it is what makes the ordinary post-pull-request
    case work instead of raising.

    ``None`` when the graph names no such node, or names more than one: a graph
    that has not said where to re-enter has not asked to be reset, and guessing
    between two candidates would reset it somewhere arbitrary.
    """
    topology = runtime.topology(snapshot.graph_id)
    open_nodes = [node for node in topology.nodes if node.always_open] if topology else []
    return open_nodes[0].node_id if len(open_nodes) == 1 else None


def create_app(
    session: AgentSession,
    runners: Mapping[str, AgentRunner],
    static_directory: Path | None = None,
    *,
    workflow_catalog: WorkflowCatalog | None = None,
    graph_runtime: AbstractAsyncContextManager[GraphRuntime] | None = None,
    approval_policy: ApprovalConfig = ApprovalConfig(),
    credential_store: GitHubCredentialStore | None = None,
    github_client_id: str = "",
    github_client_id_source: str = "configuration",
    github_login_config: GitHubLoginConfig | None = None,
    source_control_preferences: SourceControlPreferences | None = None,
    slack_credential_store: SlackCredentialStore | None = None,
    github_webhook_secret: Callable[[], str] = lambda: "",
    github_repository: str = "",
    github_bot_login: str = "",
    github_comment_handler: Callable[[GithubComment], Awaitable[None]] | None = None,
    communications_channel: str = "",
    public_url: str = "",
    work_orders: WorkOrdersConfig = WorkOrdersConfig(),
    utilization: UtilizationService | None = None,
    milestone_scoper: MilestoneScoping | None = None,
    concierge_provider: ACPAgentProvider | None = None,
) -> Starlette:
    """Build the web application around already-composed capabilities."""
    if workflow_catalog is None:
        loaded_config = load_engine_config()
        catalog = (
            load_workflow_catalog(loaded_config.workflows_directory)
            if loaded_config.workflows_directory is not None
            else WorkflowCatalog.from_graphs(())
        )
    else:
        catalog = workflow_catalog
    # The graph workflows this deployment could run, looked up by the id the
    # dropdown sends back.
    #
    # "Could", not "does": whether they are actually offered is `offered_graphs`
    # below, which additionally asks whether the engine is running.
    graph_workflows: Mapping[str, GraphWorkflow] = (
        {str(graph.graph_id): graph for graph in catalog.graphs}
        if graph_runtime is not None
        else {}
    )
    surface = _GraphSurface()
    # Filled by the graph engine while a run is going, and read by the feed the
    # graph's own sub-application serves. Built here rather than when the
    # server starts so that the observer below can be written once.
    graph_events = EventLog()

    def offered_graphs() -> Mapping[str, GraphWorkflow]:
        """The graph entries a person may pick, right now.

        Two things have to be true, and the second one is only knowable once
        the server is up: this deployment has graph workflows, and the engine
        that runs them opened. If it did not -- an unwritable state directory,
        a graph that no longer compiles -- there are no graph entries at
        all, rather than entries that fail the moment somebody picks one.
        """
        return graph_workflows if surface.runtime is not None else {}

    approval_feed = ApprovalFeed(session.state_store)
    service = ThreadService(
        session,
        runners,
        approval_policy,
        approval_observer=approval_feed.publish,
    )
    run_reader = RunReader(session.state_store, catalog)

    pending_graph_notifications: dict[RunId, list[RuntimeEvent]] = {}
    graph_notification_lock = asyncio.Lock()

    async def notify_graph_event(state: RunState, event: RuntimeEvent) -> None:
        """Report lifecycle events without making delivery failure fail the graph."""
        text = ""
        mention = False
        if state.origin is None:
            return
        topology = (
            surface.runtime.topology(GraphId(str(state.workflow_id)))
            if surface.runtime else None
        )
        node = topology.node(event.node_id) if topology and event.node_id else None
        label = node.name if node else str(event.node_id or "Workflow")
        if event.kind is EventKind.NODE_STARTED:
            text = f"*{label}* started."
        elif event.kind is EventKind.APPROVAL_REQUESTED:
            if event.payload.get("toolName") == "human_review":
                text = "Review complete and ready for your decision."
            else:
                text = f"*{label}* needs your approval: {event.payload.get('reason', '')}"
            mention = True
        elif event.kind is EventKind.RUN_FAILED:
            text = f"Work order failed: {event.payload.get('error', 'Unknown error')}"
            mention = True
        elif event.kind is EventKind.RUN_FINISHED:
            text = "Work order finished."
        if text:
            link = run_notifier.work_order_link(state)
            await run_notifier.announce(
                state, text, links=(link,) if link else (), mention=mention,
            )

    async def graph_notifications(event: RuntimeEvent) -> None:
        if event.kind not in (
            EventKind.NODE_STARTED, EventKind.APPROVAL_REQUESTED,
            EventKind.RUN_FAILED, EventKind.RUN_FINISHED,
        ):
            return
        async with graph_notification_lock:
            state = await session.state_store.load(event.run_id)
            if state is None:
                pending_graph_notifications.setdefault(event.run_id, []).append(event)
                return
            for pending in pending_graph_notifications.pop(event.run_id, []):
                await notify_graph_event(state, pending)
            await notify_graph_event(state, event)

    async def graph_event(event: RuntimeEvent) -> None:
        """Everything the graph engine says, kept where two readers can see it.

        The feed is one reader: a browser or a script watching a graph run gets
        these back in order from the sub-application below.

        The WorkOrder row is the other. A graph run keeps its real progress in
        the graph engine's own files, and this app only holds a row for it, so
        without this the row would say "an agent is working" long after the run
        had finished or fallen over. Restarts, endings, and the name produced by
        a naming node are copied across. The rest of what a graph says is about
        positions inside the graph, and a row has nowhere to put it.
        """
        await graph_events.append(event)
        await graph_notifications(event)
        phase = GRAPH_EVENT_PHASES.get(event.kind)
        name = _graph_workorder_name(event.payload.get("values"))
        if phase is None and not name:
            return
        state = await session.state_store.load(event.run_id)
        if state is None:
            return
        updated = replace(
            state,
            name=name or state.name,
            phase=phase or state.phase,
            failure_reason=(
                "" if event.kind is EventKind.RUN_FORKED
                else str(event.payload.get("error", "")) or state.failure_reason
            ),
        )
        if updated != state:
            await session.state_store.save(updated)

    async def restore_graph_runs(runtime: GraphRuntime) -> None:
        """Pick every unfinished graph WorkOrder back up, or say why it cannot be.

        A run's progress lives in the graph engine's files, but the *driver* --
        the thing actually working through the graph -- is a task in a process,
        and a process that stops takes its drivers with it. Nothing rebuilds
        them on its own, so without this a run that was mid-agent when the
        server was restarted would sit at "working" forever, saying nothing and
        doing nothing.

        Three answers, one per thing the engine can say about a run:

        * **working** -- there is no driver for it in this fresh process, so it
          is sent back to the last position it saved and carried on from there.
          Whatever the interrupted agent had done since that position is lost,
          which is the honest cost of the process having died mid-sentence;
        * **waiting on a person** -- left exactly as it is. Answering the
          question is what starts it again, and that already works;
        * **finished or failed** -- the row missed the ending because the
          process was gone when it was announced, so it is copied over now.

        A run the engine has never heard of is one whose state was deleted from
        under it. It cannot be recovered and cannot be waited for, so the row is
        failed with a reason rather than left claiming to be working.

        So is a run of a workflow this deployment no longer has -- withdrawn,
        or renamed without retiring the id it had. Nothing can pick that one up
        either, and a row left mid-flight would say an agent is working on it
        forever, so it is failed saying which workflow went missing.
        """
        for state in await session.state_store.list_runs():
            if state.is_terminal or state.phase is RunPhase.SCHEDULED:
                continue
            try:
                try:
                    snapshot = await runtime.snapshot(state.run_id)
                except UnknownGraphError:
                    await session.state_store.save(
                        replace(
                            state,
                            phase=RunPhase.FAILED,
                            failure_reason=(
                                "the workflow this WorkOrder ran, "
                                f"{state.workflow_id}, is no longer available"
                            ),
                        )
                    )
                    continue
                if snapshot is None:
                    await session.state_store.save(
                        replace(
                            state,
                            phase=RunPhase.FAILED,
                            failure_reason=(
                                "the graph engine has no record of this run"
                            ),
                        )
                    )
                    continue
                if snapshot.status is RunStatus.RUNNING:
                    # A restart is the only way to be here: a run that is
                    # working has a driver, and this runs before any request
                    # could have started one.
                    if snapshot.checkpoint_id is None:
                        continue
                    await runtime.resume_from(state.run_id, snapshot.checkpoint_id)
                    continue
                phase = GRAPH_PHASES[snapshot.status]
                name = _graph_workorder_name(snapshot.values)
                if (
                    phase is not state.phase
                    or snapshot.error != state.failure_reason
                    or (name and name != state.name)
                ):
                    await session.state_store.save(
                        replace(
                            state,
                            name=name or state.name,
                            phase=phase,
                            failure_reason=snapshot.error or state.failure_reason,
                        )
                    )
            except Exception:
                # One unrecoverable run must not stop the others from being
                # recovered, and none of them may stop the server from serving.
                log.exception("could not restore graph WorkOrder %s", state.run_id)

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        async with AsyncExitStack() as opened:
            opened.push_async_callback(slack_ingress.close)
            opened.push_async_callback(github_concierge.close)
            opened.push_async_callback(github_ingress.close)
            if graph_runtime is not None:
                # Opening the graph engine is what makes a graph WorkOrder
                # startable: it compiles every graph in the workflow directory
                # and opens the files they remember their progress in. The exit
                # stack closes it again when the server stops, which is the
                # only thing that closes those files.
                #
                # It can fail two ways, and they are not the same kind of news.
                #
                # A graph that does not compile is a broken definition: a file
                # in this deployment's workflow directory says something that is
                # not a graph. Nothing about it improves by carrying on, and a
                # server that quietly dropped it would be running a deployment
                # nobody configured. So the graph is named, the reason is logged
                # in full, and startup fails -- loudly, at the moment somebody
                # is looking, rather than the first time a person picks it.
                #
                # Anything else is the environment around the graphs rather than
                # the graphs themselves: a state directory this process cannot
                # write, a checkpoint file another process is holding. That is
                # not a reason for chats, projects and the step WorkOrders to go
                # down with it, so it is logged and contained -- no engine, and
                # therefore no graph entries offered anywhere.
                try:
                    surface.runtime = await opened.enter_async_context(graph_runtime)
                except GraphCompilationError as broken:
                    log.error(
                        "graph workflow %r does not compile, so this server "
                        "will not start: %s",
                        str(broken.graph_id),
                        broken.reason,
                        exc_info=True,
                    )
                    raise
                except Exception:
                    log.exception(
                        "the graph engine did not start; graph WorkOrders are "
                        "not being offered in this process"
                    )
                else:
                    # The graph engine's own control surface, so a run started
                    # here can be watched and answered. Built first because it
                    # installs a listener of its own, and this app wants that
                    # listener *and* the WorkOrder row kept up to date -- so
                    # ours is installed afterwards and does both.
                    surface.app = create_graph_app(surface.runtime, graph_events)
                    surface.runtime.observe(graph_event)
                    await restore_graph_runs(surface.runtime)
            yield

    async def config(_request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "agents": [
                    {
                        "id": str(agent_id),
                        "description": profile.description,
                        "instructions": profile.instructions,
                    }
                    for agent_id, profile in sorted(session.profiles.items())
                ],
                "runners": [
                    {"id": name, "implementation": type(runner).__name__}
                    for name, runner in runners.items()
                ],
                "defaultAgent": str(next(iter(sorted(session.profiles)))),
                # Which agent the New Project button starts a conversation with, named
                # here rather than in the client so the id stays one thing this
                # process owns. Empty when no such profile is composed, which is
                # the client's cue that there is nothing to plan with.
                "planAgent": (
                    str(PLANNER.agent_id)
                    if PLANNER.agent_id in session.profiles
                    else ""
                ),
                "defaultRunner": session.default_runner,
                # Only the graphs this process can actually start are here --
                # see `offered_graphs` -- because an entry nobody could run
                # would be a choice that fails after it was made. Their
                # creation fields come from the workflow's own declarations.
                "workflows": [
                    {
                        "id": str(graph.graph_id),
                        "name": graph.name,
                        **(
                            {"inputs": [asdict(item) for item in graph.inputs]}
                            if getattr(graph, "inputs", ()) else {}
                        ),
                    }
                    for graph in offered_graphs().values()
                ],
            }
        )

    async def list_threads(_request: Request) -> JSONResponse:
        return JSONResponse(
            {"threads": [_thread_json(t) for t in await service.list()]}
        )

    async def list_runs(_request: Request) -> JSONResponse:
        runs = []
        for run in await run_reader.list():
            row = _run_json(run, listing=True)
            # Carry only the live frontier and approval owners, not the
            # snapshot's potentially large values.
            if (
                run.phase not in {"scheduled", "succeeded", "failed"}
                and surface.runtime is not None
            ):
                # A row of a workflow this deployment no longer has cannot
                # report a frontier, and the list is every WorkOrder there is:
                # letting that refusal out would take the whole page down over
                # one old row, rather than showing it without its progress.
                try:
                    snapshot = await surface.runtime.snapshot(run.run_id)
                except UnknownGraphError:
                    snapshot = None
                if snapshot is not None:
                    row["graphProgress"] = {
                        "activeNodeIds": list(
                            dict.fromkeys(
                                str(one.node_id) for one in snapshot.active_executions
                            )
                        ),
                        "waitingNodeIds": list(
                            dict.fromkeys(
                                str(one.node_id) for one in snapshot.pending_approvals
                            )
                        ),
                        "nextNodeIds": [str(node) for node in snapshot.next_nodes],
                    }
            runs.append(row)
        return JSONResponse({"runs": runs})

    async def open_conversations() -> set[AgentInstanceId]:
        """The plans a project row can be linked to.

        A project is reached through the planning conversation it was named
        after. Resolved against the threads that can be opened rather than
        spelled from the id alone: a project recorded some other way has the
        same shape and no conversation, and an archived plan's page is a blank
        new chat rather than the plan.
        """
        return {
            thread.instance_id for thread in await service.list() if not thread.archived
        }

    async def list_projects(_request: Request) -> JSONResponse:
        projects = await session.state_store.list_projects()
        conversations = await open_conversations()
        # A project's milestones are offered in the rail only by the projects
        # that have some, so the list says how many. Counted by the store, in
        # one grouped query: the shell polls this route every second, and both
        # a query per row and a read of every milestone would make that cost
        # grow -- with the list, or with the size of every plan in it.
        milestones = await session.state_store.count_milestones_by_project()
        return JSONResponse(
            {
                "projects": [
                    _project_json(
                        project,
                        conversations,
                        milestones=milestones.get(project.project_id, 0),
                    )
                    for project in projects
                ]
            }
        )

    async def create_project(request: Request) -> JSONResponse:
        body = await _json_body(request)
        try:
            name = _required_string(body, "name")
        except ValueError as error:
            return _error(str(error), 400)
        project = Project(ProjectId(f"project-{uuid4().hex[:12]}"), name[:80])
        await session.state_store.save_project(project)
        # Recorded rather than planned, so it owns no conversation to link to
        # and nothing has been planned under it yet.
        return JSONResponse(_project_json(project, (), milestones=0), status_code=201)

    async def archive_project(request: Request) -> JSONResponse:
        """Put a project away, or take it back out, from one pair of routes.

        Which one was asked for is read from the path, the way archiving a chat
        already is: the two differ only in the flag they record.
        """
        project_id = ProjectId(request.path_params["project_id"])
        project = await session.state_store.load_project(project_id)
        if project is None:
            return _error("project not found", 404)
        archived = request.url.path.rsplit("/", 1)[-1] == "archive"
        project = replace(project, archived=archived)
        await session.state_store.save_project(project)
        # An archived project keeps its plan: restoring puts the link and the
        # milestones back rather than leaving a row that has forgotten where it
        # went. The answer is the whole row the list would send -- counted the
        # same way, so the two cannot drift -- and a client that redraws from it
        # is not left with a project missing half itself.
        counts = await session.state_store.count_milestones_by_project()
        return JSONResponse(
            _project_json(
                project,
                await open_conversations(),
                milestones=counts.get(project_id, 0),
            )
        )

    async def list_project_milestones(request: Request) -> JSONResponse:
        project_id = ProjectId(request.path_params["project_id"])
        project = await session.state_store.load_project(project_id)
        if project is None:
            return _error("project not found", 404)
        milestones = await session.state_store.list_milestones(project_id)
        return JSONResponse(
            {
                # Linked to its plan like any other row: the milestones page
                # this answers is where the way back to the conversation is.
                "project": _project_json(project, await open_conversations()),
                "milestones": [_milestone_json(milestone) for milestone in milestones],
            }
        )

    async def scope_milestone(request: Request) -> JSONResponse:
        """Run the configured ACP scoper and return its structured plan."""
        project_id = ProjectId(request.path_params["project_id"])
        milestone_id = MilestoneId(request.path_params["milestone_id"])
        project = await session.state_store.load_project(project_id)
        milestone = await session.state_store.load_milestone(milestone_id)
        if project is None:
            return _error("project not found", 404)
        if milestone is None or milestone.project_id != project_id:
            return _error("milestone not found", 404)
        body = await _json_body(request)
        try:
            message = _required_string(body, "message")
        except ValueError as error:
            return _error(str(error), 400)

        current = tuple(
            _workorder_for_run(run, milestone_id)
            for run in await session.state_store.list_runs(milestone_id)
        )
        if milestone_scoper is None:
            return _error("milestone scoping is not configured", 503)
        plan = await milestone_scoper.run(
            workorders=current,
            milestone=MilestoneScope(
                milestone_id=milestone.milestone_id,
                requirements=(milestone.description,) if milestone.description else (),
                dependencies=milestone.dependencies,
                name=milestone.name,
            ),
            policy=ScopingPolicy(rules=(message,)),
        )
        # Scope proposals become durable work, but dispatch is an explicit action.
        definition = _mentioned_workflow()
        workflow_id = WorkflowId(
            work_orders.workflow or (str(definition.graph_id) if definition else "")
        )
        for spec in plan.create:
            if spec.milestone_id != milestone_id:
                return _error("scoper proposed work for another milestone", 400)
        for spec in plan.create:
            prompt = spec.objective
            if spec.evidence_requirements:
                prompt += "\n\nEvidence requirements:\n" + "\n".join(spec.evidence_requirements)
            if spec.dependencies:
                prompt += "\n\nDepends on: " + ", ".join(spec.dependencies)
            await session.state_store.save(RunState(
                run_id=RunId(f"run-{uuid4().hex[:12]}"),
                task_id=TaskId(f"task-{uuid4().hex[:12]}"),
                workflow_id=workflow_id,
                milestone_id=milestone_id,
                phase=RunPhase.SCHEDULED,
                name=spec.name,
                prompt=prompt,
                repository=work_orders.repository,
            ))
        return JSONResponse(_scoping_plan_json(plan))

    async def start_graph_run(
        runtime: GraphRuntime,
        graph: GraphWorkflow,
        *,
        inputs: dict[str, str],
        prompt: str,
        repository: str,
        milestone_id: MilestoneId | None,
        origin: RunOrigin | None = None,
        scheduled: RunState | None = None,
    ) -> RunState:
        """Hand a graph WorkOrder to the graph engine and keep a row for it.

        What actually starts the work is one call: the graph engine is given
        the graph's id and the two things every one of these graphs asks for --
        the task to do, and the repository to do it in. It provisions the
        checkout, runs the agents and stops for a person by itself, and it
        remembers all of that in its own files.

        The row saved afterwards is this app's, and it is a record rather than
        a driver: it is what puts the WorkOrder in the list, on the sidebar and
        at a URL. It carries the graph engine's own run id, so the two halves
        are talking about the same run and nothing has to translate between two
        sets of ids.

        Declared inputs are validated before starting and carried in graph state.

        The engine is an argument rather than something read here, because
        having one is what made this graph offerable in the first place: a
        caller that got a graph out of `offered_graphs` has already established
        that the engine is running, and passing it on says so.
        """
        snapshot = await runtime.start(
            GraphId(str(graph.graph_id)),
            {
                "task": prompt,
                "repository": repository,
                **({"inputs": inputs} if inputs else {}),
            },
            run_id=scheduled.run_id if scheduled else None,
        )
        if approval_policy.auto_approve:
            topology = runtime.topology(GraphId(str(graph.graph_id)))
            if topology is not None:
                for node in topology.nodes:
                    await runtime.set_auto_approve(snapshot.run_id, node.node_id, True)
        state = RunState(
            run_id=snapshot.run_id,
            task_id=scheduled.task_id if scheduled else TaskId(f"task-{uuid4().hex[:12]}"),
            name=scheduled.name if scheduled else "",
            workflow_id=WorkflowId(str(graph.graph_id)),
            milestone_id=milestone_id,
            # Working, as the engine has just reported it. `graph_event` above
            # moves this when the run ends.
            phase=GRAPH_PHASES[snapshot.status],
            prompt=prompt,
            repository=repository,
            origin=origin,
        )
        await session.state_store.save(state)
        # Nodes may publish before start() returns and before the origin exists.
        async with graph_notification_lock:
            for event in pending_graph_notifications.pop(state.run_id, []):
                await notify_graph_event(state, event)
        # A very short run can be over before the row above exists, and the
        # ending it announced would then have had nothing to land on -- leaving
        # a WorkOrder that claims to be working forever. So the engine is asked
        # once more, now that there is a row for its answer.
        latest = await runtime.snapshot(state.run_id)
        latest_name = (
            _graph_workorder_name(latest.values) if latest is not None else ""
        )
        if latest is not None and (
            GRAPH_PHASES[latest.status] is not state.phase
            or (latest_name and latest_name != state.name)
        ):
            state = replace(
                state,
                name=latest_name or state.name,
                phase=GRAPH_PHASES[latest.status],
                failure_reason=latest.error,
            )
            await session.state_store.save(state)
        return state

    scheduled_start_lock = asyncio.Lock()

    async def start_scheduled_run(request: Request) -> JSONResponse:
        async with scheduled_start_lock:
            state = await session.state_store.load(RunId(request.path_params["run_id"]))
            if state is None:
                return _error("run not found", 404)
            if state.phase is not RunPhase.SCHEDULED:
                return _error("workorder is already started", 409)
            workflow_id = state.workflow_id or WorkflowId(work_orders.workflow)
            graph = offered_graphs().get(str(workflow_id)) if workflow_id else _mentioned_workflow()
            if graph is None:
                return _error("configure work_orders.workflow before starting this workorder", 400)
            repository = state.repository or work_orders.repository
            if not repository:
                return _error("configure work_orders.repository before starting this workorder", 400)
            assert surface.runtime is not None
            try:
                inputs = resolve_inputs(getattr(graph, "inputs", ()), {})
            except ValueError as error:
                return _error(str(error), 400)
            state = await start_graph_run(
                surface.runtime, graph, inputs=inputs, prompt=state.prompt,
                repository=repository, milestone_id=state.milestone_id,
                scheduled=state,
            )
            run = await run_reader.get(state.run_id)
            assert run is not None
            return JSONResponse(_run_json(run))

    async def create_run(request: Request) -> JSONResponse:
        """Persist a workflow request and start its supported local execution."""
        body = await _json_body(request)
        try:
            prompt = _required_string(body, "prompt")
            repository = _required_string(body, "repository")
            workflow_id = WorkflowId(_required_string(body, "workflowId"))
            milestone_value = _optional_string(body, "milestoneId")
        except ValueError as error:
            return _error(str(error), 400)
        graph = offered_graphs().get(str(workflow_id))
        if graph is None:
            return _error(f"unknown workflow definition: {workflow_id}", 400)
        milestone_id = (
            MilestoneId(milestone_value) if milestone_value is not None else None
        )
        if milestone_id is not None:
            milestone = await session.state_store.load_milestone(milestone_id)
            if milestone is None:
                return _error(f"unknown milestone: {milestone_id}", 400)

        # `offered_graphs` only answers with a graph while the engine is
        # running, so this cannot be `None` here.
        assert surface.runtime is not None
        try:
            inputs = resolve_inputs(
                getattr(graph, "inputs", ()), body.get("inputs", {})
            )
        except ValueError as error:
            return _error(str(error), 400)
        state = await start_graph_run(
            surface.runtime,
            graph,
            inputs=inputs,
            prompt=prompt,
            repository=repository,
            milestone_id=milestone_id,
        )
        run = await run_reader.get(state.run_id)
        assert run is not None
        return JSONResponse(_run_json(run), status_code=201)

    async def get_run(request: Request) -> JSONResponse:
        run_id = RunId(request.path_params["run_id"])
        run = await run_reader.get(run_id)
        if run is None:
            return _error("run not found", 404)
        return JSONResponse(_run_json(run))

    async def delete_run(request: Request) -> Response:
        """Throw a WorkOrder away, whatever it was in the middle of.

        A run still being worked on is the graph engine's, and its driver is a
        task in the engine rather than anything this app holds. Deleting the
        row without telling the engine would take the WorkOrder off the rail
        and leave the run working -- agents still going in the repository, with
        nothing left on screen to stop them by. So the engine is asked to
        cancel the run, and only then is the row forgotten.
        """
        run_id = RunId(request.path_params["run_id"])
        state = await session.state_store.load(run_id)
        if state is None:
            return _error("run not found", 404)
        if state.phase is not RunPhase.SCHEDULED:
            await cancel_graph_run(run_id)
        await session.state_store.delete_run(run_id)
        return Response(status_code=204)

    async def cancel_graph_run(run_id: RunId) -> None:
        """Stop a graph WorkOrder in the engine, if there is one to stop.

        Two ways there is nothing to do, and neither is a reason to refuse the
        delete. The engine may not be running at all -- it failed to open, or
        this process never had one -- in which case nothing here is driving the
        run either, because a driver is a task in a process. And the engine may
        not know the run: a row whose graph state was deleted from under it,
        which `restore_graph_runs` fails on startup for the same reason.

        Either way the row is the reader's to throw away, so the reason is
        logged and the delete goes on. Refusing would leave a WorkOrder nobody
        can remove and nothing is working on.
        """
        runtime = surface.runtime
        if runtime is None:
            log.warning(
                "the graph engine is not running, so graph WorkOrder %s was "
                "deleted without being cancelled",
                run_id,
            )
            return
        try:
            await runtime.cancel(run_id)
        except GraphRuntimeError:
            log.warning(
                "the graph engine has no record of graph WorkOrder %s, so "
                "there was nothing to cancel",
                run_id,
            )

    async def graph_run_events(request: Request) -> JSONResponse:
        """Replay the graph transcript for the WorkOrder UI.

        The graph control surface deliberately exposes a live event stream. The
        WorkOrder page also needs a finite snapshot when it opens after an
        agent has finished, so serve the same recorded events as JSON here.

        Answered for any WorkOrder, including one whose workflow this
        deployment no longer has: what a run said is recorded against the run,
        not against the graph, so a withdrawn or renamed workflow takes away
        the ability to draw the graph and not the transcripts underneath it.
        """
        run_id = RunId(request.path_params["run_id"])
        if await session.state_store.load(run_id) is None:
            return _error("run not found", 404)
        return JSONResponse(
            {
                "events": [
                    {
                        "sequence": event.sequence,
                        "type": event.kind.value,
                        "nodeId": str(event.node_id) if event.node_id else None,
                        "payload": dict(event.payload),
                    }
                    for event in graph_events.since(run_id)
                ]
            }
        )

    async def create_thread(request: Request) -> JSONResponse:
        body = await _json_body(request)
        create_project = body.get("createProject", False)
        if not isinstance(create_project, bool):
            return _error("createProject must be a boolean", 400)
        try:
            thread = await service.create(
                AgentId(_required_string(body, "agentId")),
                _required_string(body, "runner"),
            )
        except (KeyError, ValueError) as error:
            return _error(str(error), 400)
        if create_project:
            # Finish the durable intent before returning the thread id. The
            # client cannot replace /plan with its permalink until this request
            # resolves, so closing or reloading during the slower title request
            # cannot strand an ordinary chat without its project.
            thread = await service.update_metadata(
                thread.instance_id, title="New project"
            )
            await session.state_store.save_project(
                Project(project_id_for_instance(thread.instance_id), thread.title)
            )
        return JSONResponse(_thread_json(thread), status_code=201)

    async def get_thread(request: Request) -> JSONResponse:
        thread = await service.get(_thread_id(request))
        if thread is None:
            return _error("thread not found", 404)
        return JSONResponse(_thread_json(thread))

    async def update_thread(request: Request) -> JSONResponse:
        instance_id = _thread_id(request)
        thread = await service.get(instance_id)
        if thread is None:
            return _error("thread not found", 404)
        body = await _json_body(request)
        title = None
        if "title" in body:
            title = str(body["title"]).strip()
            if title:
                title = title[:80]
            else:
                title = None
        runner = str(body["runner"]) if "runner" in body else None
        try:
            thread = await service.update_metadata(
                instance_id, title=title, runner=runner
            )
        except ValueError as error:
            return _error(str(error), 400)
        return JSONResponse(_thread_json(thread))

    async def archive_thread(request: Request) -> JSONResponse:
        thread = await service.get(_thread_id(request))
        if thread is None:
            return _error("thread not found", 404)
        thread = await service.update_metadata(
            thread.instance_id,
            archived=request.url.path.rsplit("/", 1)[-1] == "archive",
        )
        return JSONResponse(_thread_json(thread))

    async def delete_thread(request: Request) -> Response:
        instance_id = _thread_id(request)
        if await service.get(instance_id) is None:
            return _error("thread not found", 404)
        await service.delete(instance_id)
        return Response(status_code=204)

    async def messages(request: Request) -> JSONResponse:
        instance_id = _thread_id(request)
        thread = await service.get(instance_id)
        if thread is None:
            return _error("thread not found", 404)
        history = await service.history(instance_id)
        active = service.active_run(instance_id)
        # What a conversation was asked to allow is part of the transcript, and
        # is loaded with it. The run stream replays these too, but it is only
        # opened while this process is still executing the turn -- so a chat
        # whose run has since finished used to come back from a page load with
        # its approvals missing entirely.
        approvals = await session.state_store.list_approvals(instance_id=instance_id)
        return JSONResponse(
            {
                "messages": _messages_json(history),
                "approvals": [_approval_json(record) for record in approvals],
                # A complete assistant transcript can become durable just
                # before ActiveRun flips to done. In that window replaying it
                # would duplicate the assistant message in the client.
                "unstable_resume": (
                    active is not None
                    and bool(history)
                    and history[-1].role is Role.USER
                ),
            }
        )

    async def approval_events(request: Request) -> Response:
        instance_id = _thread_id(request)
        if await service.get(instance_id) is None:
            return _error("thread not found", 404)
        return StreamingResponse(
            approval_feed.stream(instance_id),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    async def title_thread(request: Request) -> JSONResponse:
        instance_id = _thread_id(request)
        thread = await service.get(instance_id)
        if thread is None:
            return _error("thread not found", 404)
        body = await _json_body(request)
        opening_text = str(body["text"]).strip() if body.get("text") else None
        runner = str(body["runner"]) if body.get("runner") else None
        try:
            title = await service.generate_title(instance_id, opening_text, runner)
        except ValueError as error:
            return _error(str(error), 400)
        except Exception as failure:
            # A provider that cannot name the chat has not cost anybody
            # anything yet, and must not be allowed to. The client asks for a
            # name *before* sending the message being named, so a failure
            # answered with a 500 here would take the user's turn with it --
            # a CLI that is out of quota would stop the chat working rather
            # than leave it called "New chat".
            #
            # The reason travels in the body instead of the status, because
            # something did go wrong and the placeholder name is not evidence
            # of which provider failed or why.
            return JSONResponse({"title": thread.title, "error": str(failure)})
        return JSONResponse({"title": title})

    async def attach_workspace(request: Request) -> JSONResponse:
        instance_id = _thread_id(request)
        if await service.get(instance_id) is None:
            return _error("thread not found", 404)
        try:
            thread = await service.attach_workspace(instance_id)
        except RuntimeError as error:
            # A repository that cannot produce a checkout -- unwired, or git
            # refusing -- is the server's problem to explain, not a 404.
            return _error(str(error), 409)
        return JSONResponse(_thread_json(thread))

    async def detach_workspace(request: Request) -> JSONResponse:
        instance_id = _thread_id(request)
        thread = await service.get(instance_id)
        if thread is None:
            return _error("thread not found", 404)
        try:
            thread = await service.detach_workspace(instance_id)
        except RuntimeError as error:
            return _error(str(error), 409)
        return JSONResponse(_thread_json(thread))

    async def run_thread(request: Request) -> Response:
        instance_id = _thread_id(request)
        if await service.get(instance_id) is None:
            return _error("thread not found", 404)
        body = await _json_body(request)
        try:
            text = _required_string(body, "text")
        except ValueError as error:
            return _error(str(error), 400)
        runner = str(body["runner"]) if body.get("runner") else None

        try:
            run = await service.start_run(instance_id, text, runner)
            return StreamingResponse(run.stream(), media_type="application/x-ndjson")
        except RuntimeError as error:
            return _error(str(error), 409)

    async def resume_run(request: Request) -> Response:
        instance_id = _thread_id(request)
        if await service.get(instance_id) is None:
            return _error("thread not found", 404)
        # Keep a completed snapshot available for the small race where history
        # observed an active run immediately before it finished.
        run = service.latest_run(instance_id)
        if run is not None:
            return StreamingResponse(run.stream(), media_type="application/x-ndjson")
        return Response(status_code=204)

    async def cancel_run(request: Request) -> Response:
        instance_id = _thread_id(request)
        if await service.get(instance_id) is None:
            return _error("thread not found", 404)
        try:
            await service.stop_run(instance_id)
        except RuntimeError as error:
            return _error(str(error), 409)
        return Response(status_code=204)

    async def decide_approval(request: Request) -> Response:
        instance_id = _thread_id(request)
        if await service.get(instance_id) is None:
            return _error("thread not found", 404)
        body = await _json_body(request)
        try:
            approval_id = ApprovalId(request.path_params["approval_id"])
            if "answers" in body:
                raw_answers = body["answers"]
                if not isinstance(raw_answers, dict):
                    raise ValueError("answers must be an object")
                answers = tuple(
                    UserInputAnswer(
                        question_id=str(question_id),
                        answers=tuple(values) if isinstance(values, list) else (),
                    )
                    for question_id, values in raw_answers.items()
                    if isinstance(question_id, str)
                    and isinstance(values, list)
                    and all(isinstance(value, str) for value in values)
                )
                if len(answers) != len(raw_answers):
                    raise ValueError("each answer must be an array of strings")
                approval = await service.answer_question(
                    instance_id, approval_id, answers
                )
            else:
                decision = _required_string(body, "decision")
                approval = await service.decide_approval(
                    instance_id, approval_id, decision
                )
        except ValueError as error:
            return _error(str(error), 400)
        except UnknownApprovalError as error:
            return _error(str(error), 404)
        except ApprovalDecisionNotAllowedError as error:
            return _error(str(error), 400)
        except UserInputNotAllowedError as error:
            return _error(str(error), 400)
        except ApprovalNotPendingError as error:
            # The request outlived whatever was waiting for it. Not the
            # client's mistake to fix by retrying, so not a 400.
            return _error(str(error), 409)
        return JSONResponse({"approval": _approval_json(approval)})

    # --- GitHub connection endpoints -----------------------------------------

    _credential_store = credential_store or GitHubCredentialStore()
    _source_control_preferences = (
        source_control_preferences or SourceControlPreferences()
    )

    # The single in-flight device flow. `_active_interval` tracks the current
    # polling interval, which grows when GitHub returns `slow_down`.
    _active_flow: DeviceFlowState | None = None
    _active_interval: int = 5

    def _is_local_request(request: Request) -> bool:
        """True when the request originates from the UI served by this process.

        The GitHub auth endpoints are mutating and must not be triggerable by
        arbitrary pages. Checking the Origin header against localhost is a
        lightweight CSRF guard appropriate for a local tool; it stops a
        cross-origin page from silently disconnecting the user's token or
        initiating a new device flow. GET /api/github/status is read-only and
        exempt.
        """
        origin = request.headers.get("origin", "")
        if not origin:
            # No Origin means a same-origin request (form submit, etc.) or a
            # curl call from localhost. Both are fine for a local tool.
            return True
        parsed = urlsplit(origin)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            return False
        # Browser Origin values have no path and lowercase hostnames.  Compare
        # parsed hosts rather than prefixes: localhost.evil.example is not
        # localhost.
        return parsed.hostname.lower() in {
            "localhost",
            "127.0.0.1",
            "::1",
            (request.url.hostname or "").lower(),
        }

    def _hint(value: str) -> str:
        """Return first 4 chars + bullets so the UI can confirm which ID is set."""
        return value[:4] + "••••••••" if len(value) > 4 else "••••••••"

    def _effective_client_id() -> str:
        """Env-var takes precedence; keychain is the fallback for UI-configured IDs."""
        return github_client_id or _credential_store.get_client_id() or ""

    async def github_status(_request: Request) -> JSONResponse:
        credentials = _credential_store.get_credentials()
        now = time.time()
        connected = bool(credentials and credentials.is_usable(now))
        return JSONResponse(
            {
                "connected": connected,
                "clientIdConfigured": bool(_effective_client_id()),
            }
        )

    async def source_control_status(_request: Request) -> JSONResponse:
        provider, auto_selected = source_control_settings.selected_or_detected_provider(
            _source_control_preferences
        )
        cli = source_control_settings.gh_cli_status()
        return JSONResponse(
            {
                "provider": provider,
                "autoSelected": auto_selected,
                "ghCli": {
                    "installed": cli.installed,
                    "authenticated": cli.authenticated,
                    "account": cli.account,
                    "message": cli.message,
                },
            }
        )

    async def source_control_provider_status(_request: Request) -> JSONResponse:
        """Return the chosen provider without probing an unrelated CLI.

        A saved OAuth choice is a local settings-file read.  Do not make the
        Settings panel wait for ``gh auth status`` merely to render that choice.
        First-run auto-selection still performs its one required CLI probe.
        """
        provider, auto_selected = source_control_settings.selected_or_detected_provider(
            _source_control_preferences
        )
        return JSONResponse(
            {"provider": provider, "autoSelected": auto_selected}
        )

    async def set_source_control_provider(request: Request) -> Response:
        if not _is_local_request(request):
            return _error("forbidden", 403)
        body = await request.json()
        provider = body.get("provider")
        if provider not in {"gh-cli", "github-oauth", "gitlab-oauth"}:
            return _error("provider must be 'gh-cli', 'github-oauth', or 'gitlab-oauth'", 400)
        origin = body.get("origin") if isinstance(body.get("origin"), str) else None
        if provider == "gitlab-oauth":
            try:
                origin = normalize_gitlab_origin(origin or "https://gitlab.com")
            except ValueError as error:
                return _error(str(error), 400)
        _source_control_preferences.set(provider, origin if provider == "gitlab-oauth" else None)
        return Response(status_code=204)

    async def github_get_client_id(_request: Request) -> JSONResponse:
        # Never return the actual value — only whether one is set and its hint.
        stored = _credential_store.get_client_id()
        if github_client_id:
            return JSONResponse(
                {"source": github_client_id_source, "hint": _hint(github_client_id)}
            )
        if stored:
            return JSONResponse({"source": "keychain", "hint": _hint(stored)})
        return JSONResponse({"source": "none", "hint": ""})

    async def github_set_client_id(request: Request) -> Response:
        if not _is_local_request(request):
            return _error("forbidden", 403)
        body = await request.json()
        client_id = (body.get("clientId") or "").strip()
        if not client_id:
            return _error("clientId is required", 400)
        try:
            _credential_store.set_client_id(client_id)
        except GitHubAuthError as error:
            return _error(str(error), 500)
        return Response(status_code=204)

    async def github_connect(request: Request) -> JSONResponse:
        nonlocal _active_flow, _active_interval
        if not _is_local_request(request):
            return _error("forbidden", 403)
        effective_client_id = _effective_client_id()
        if not effective_client_id:
            return _error(
                "GitHub client ID is not configured. Enter it in Settings.", 503
            )
        # Return the in-flight flow rather than discarding it — a second tab
        # or a retry gets the same codes instead of racing with any polling
        # that is still running against the first flow.
        if _active_flow is not None:
            return JSONResponse(
                {
                    "userCode": _active_flow.user_code,
                    "verificationUri": _active_flow.verification_uri,
                    "expiresIn": _active_flow.expires_in,
                    "interval": _active_interval,
                }
            )
        try:
            _active_flow = await start_device_flow(effective_client_id)
        except GitHubAuthError as error:
            return _error(str(error), 502)
        _active_interval = _active_flow.interval
        return JSONResponse(
            {
                "userCode": _active_flow.user_code,
                "verificationUri": _active_flow.verification_uri,
                "expiresIn": _active_flow.expires_in,
                "interval": _active_interval,
            }
        )

    async def github_connect_poll(request: Request) -> JSONResponse:
        nonlocal _active_flow, _active_interval
        if not _is_local_request(request):
            return _error("forbidden", 403)
        if _active_flow is None:
            return _error(
                "no active device flow; call POST /api/github/connect first", 409
            )
        try:
            result = await poll_device_flow(
                _effective_client_id(), _active_flow.device_code, _active_interval
            )
        except GitHubAuthError as error:
            _active_flow = None
            return _error(str(error), 502)
        if isinstance(result, DeviceFlowComplete):
            try:
                _credential_store.set_credentials(credentials_from_device_flow(result))
            except GitHubAuthError as error:
                _active_flow = None
                return _error(str(error), 500)
            _active_flow = None
            return JSONResponse({"status": "complete"})
        # DeviceFlowPending — update the interval in case GitHub slowed us down.
        _active_interval = result.next_interval
        return JSONResponse({"status": "pending", "nextInterval": _active_interval})

    async def github_disconnect(request: Request) -> Response:
        nonlocal _active_flow
        if not _is_local_request(request):
            return _error("forbidden", 403)
        _active_flow = None
        _credential_store.delete()
        return Response(status_code=204)

    # GitLab credentials are per OAuth issuer, unlike GitHub's single public
    # issuer.  Keep one in-flight device flow per canonical instance so tabs
    # cannot race an authorization code for the same account.
    _gitlab_flows: dict[str, tuple[object, int]] = {}

    def _gitlab_origin(request: Request | None = None, body: Mapping[str, object] | None = None) -> str:
        value = (
            body.get("origin") if body is not None else request.query_params.get("origin") if request is not None else None
        )
        try:
            return normalize_gitlab_origin(value if isinstance(value, str) else "https://gitlab.com")
        except ValueError as error:
            raise GitLabAuthError(str(error)) from error

    def _gitlab_connected(store: GitLabCredentialStore) -> bool:
        credentials = store.get_credentials()
        now = time.time()
        return bool(credentials and credentials.is_usable(now))

    async def gitlab_status(request: Request) -> JSONResponse:
        try:
            origin = _gitlab_origin(request)
        except GitLabAuthError as error:
            return _error(str(error), 400)
        store = GitLabCredentialStore(origin)
        return JSONResponse({"origin": origin, "connected": _gitlab_connected(store), "clientIdConfigured": bool(store.get_client_id())})

    async def gitlab_set_client_id(request: Request) -> Response:
        if not _is_local_request(request):
            return _error("forbidden", 403)
        body = await request.json()
        try:
            origin = _gitlab_origin(body=body)
        except GitLabAuthError as error:
            return _error(str(error), 400)
        client_id = body.get("clientId")
        if not isinstance(client_id, str) or not client_id.strip():
            return _error("clientId is required", 400)
        try:
            GitLabCredentialStore(origin).set_client_id(client_id.strip())
        except GitLabAuthError as error:
            return _error(str(error), 500)
        return Response(status_code=204)

    async def gitlab_connect(request: Request) -> JSONResponse:
        if not _is_local_request(request):
            return _error("forbidden", 403)
        body = await request.json()
        try:
            origin = _gitlab_origin(body=body)
        except GitLabAuthError as error:
            return _error(str(error), 400)
        client_id = GitLabCredentialStore(origin).get_client_id()
        if not client_id:
            return _error("GitLab client ID is not configured for this instance.", 503)
        active = _gitlab_flows.get(origin)
        if active is None:
            try:
                flow = await start_gitlab_device_flow(origin, client_id)
            except GitLabAuthError as error:
                return _error(str(error), 502)
            active = (flow, flow.interval)
            _gitlab_flows[origin] = active
        flow, interval = active
        return JSONResponse({"origin": origin, "userCode": flow.user_code, "verificationUri": flow.verification_uri, "expiresIn": flow.expires_in, "interval": interval})

    async def gitlab_connect_poll(request: Request) -> JSONResponse:
        if not _is_local_request(request):
            return _error("forbidden", 403)
        body = await request.json()
        try:
            origin = _gitlab_origin(body=body)
        except GitLabAuthError as error:
            return _error(str(error), 400)
        active = _gitlab_flows.get(origin)
        if active is None:
            return _error("no active GitLab device flow; call POST /api/gitlab/connect first", 409)
        flow, interval = active
        client_id = GitLabCredentialStore(origin).get_client_id()
        if not client_id:
            _gitlab_flows.pop(origin, None)
            return _error("GitLab client ID is not configured for this instance.", 503)
        try:
            result = await poll_gitlab_device_flow(origin, client_id, flow.device_code, interval)
        except GitLabAuthError as error:
            _gitlab_flows.pop(origin, None)
            return _error(str(error), 502)
        if isinstance(result, GitLabDeviceFlowComplete):
            try:
                GitLabCredentialStore(origin).set_credentials(gitlab_credentials_from_device_flow(result))
            except GitLabAuthError as error:
                _gitlab_flows.pop(origin, None)
                return _error(str(error), 500)
            _gitlab_flows.pop(origin, None)
            return JSONResponse({"status": "complete"})
        _gitlab_flows[origin] = (flow, result.next_interval)
        return JSONResponse({"status": "pending", "nextInterval": result.next_interval})

    async def gitlab_disconnect(request: Request) -> Response:
        if not _is_local_request(request):
            return _error("forbidden", 403)
        body = await request.json()
        try:
            origin = _gitlab_origin(body=body)
        except GitLabAuthError as error:
            return _error(str(error), 400)
        _gitlab_flows.pop(origin, None)
        GitLabCredentialStore(origin).delete()
        return Response(status_code=204)

    async def graph_surface(scope: Scope, receive: Receive, send: Send) -> None:
        """Pass anything under `/graph` to the graph engine's own server.

        The graph engine ships a small API of its own -- what a run is doing,
        what it has raised, and the two things a person can send back: a
        message for whichever agent is working, and an answer to a question it
        stopped on. That is how a graph run gets approved today, and this
        app's pages cannot do it yet.

        A hop rather than a re-implementation, and behind a prefix of its own
        because both servers call their runs `/api/runs`. It has to be a
        forwarder rather than a plain mount because the engine on the far side
        does not exist until the server starts.
        """
        if surface.app is None:
            await JSONResponse(
                {"error": "this process is not running graph workflows"},
                status_code=503,
            )(scope, receive, send)
            return
        await surface.app(scope, receive, send)

    # --- Slack connection endpoints ------------------------------------------

    _slack_store = slack_credential_store or SlackCredentialStore()
    _slack_state: str | None = None
    _slack_redirect_uri: str | None = None
    # The way back into a chat thread, for the one message this app sends
    # itself: the reply that says a mention became a work order. Everything
    # after that is the executor's, which builds its own from the same port.
    run_notifier = RunNotifier(session.capabilities.communications, public_url)

    def _signing_secret() -> str:
        return _slack_store.signing_secret() or ""

    async def slack_status(_request: Request) -> JSONResponse:
        credentials = _slack_store.credentials()
        signing_secret = bool(_signing_secret())
        connected = bool(_slack_store.token())
        return JSONResponse(
            {
                "configured": credentials is not None,
                "connected": connected,
                # Whether a mention could actually start something, and which
                # of its parts is missing -- so the settings panel can offer
                # the one this deployment still needs rather than a paragraph
                # listing everything it might. Being connected counts: a work
                # order this server cannot reply to is one nobody would see.
                "events": (
                    connected and signing_secret and bool(work_orders.repository)
                ),
                "signingSecret": signing_secret,
            }
        )

    async def slack_set_credentials(request: Request) -> Response:
        nonlocal _slack_state, _slack_redirect_uri
        if not _is_local_request(request):
            return _error("forbidden", 403)
        body = await request.json()
        client_id = (body.get("clientId") or "").strip()
        client_secret = (body.get("clientSecret") or "").strip()
        signing_secret = (body.get("signingSecret") or "").strip()
        if signing_secret and not client_id and not client_secret:
            # Adding only the signing secret, to a deployment that connected
            # before it was asked for. It belongs to the app already
            # configured, so this must not walk the path below: revoking the
            # token and re-saving the same OAuth pair would cost a working
            # connection to enable mentions on it.
            if _slack_store.credentials() is None:
                return _error("Slack OAuth credentials are not configured", 409)
            try:
                _slack_store.set_signing_secret(signing_secret)
            except SlackAuthError as error:
                return _error(str(error), 500)
            return Response(status_code=204)
        if not client_id or not client_secret:
            return _error("clientId and clientSecret are required", 400)
        token = _slack_store.token()
        if token:
            try:
                await revoke_slack_token(token)
            except SlackAuthError as error:
                return _error(str(error), 502)
            _slack_store.disconnect()
        try:
            _slack_store.set_credentials(client_id, client_secret)
            if signing_secret:
                # After the credentials, never before: saving them forgets the
                # previous app's signing secret, which would take this one too.
                _slack_store.set_signing_secret(signing_secret)
        except SlackAuthError as error:
            return _error(str(error), 500)
        _slack_state = None
        _slack_redirect_uri = None
        return Response(status_code=204)

    async def slack_connect(request: Request) -> JSONResponse:
        nonlocal _slack_state, _slack_redirect_uri
        if not _is_local_request(request):
            return _error("forbidden", 403)
        credentials = _slack_store.credentials()
        if credentials is None:
            return _error("Slack OAuth credentials are not configured", 503)
        _slack_state = uuid4().hex
        _slack_redirect_uri = str(request.url_for("slack_callback"))
        return JSONResponse(
            {"authorizationUrl": slack_authorization_url(credentials.client_id, _slack_redirect_uri, _slack_state)}
        )

    async def slack_callback(request: Request) -> Response:
        nonlocal _slack_state, _slack_redirect_uri
        if not _slack_state or request.query_params.get("state") != _slack_state:
            return _error("invalid OAuth state", 400)
        code = request.query_params.get("code")
        credentials = _slack_store.credentials()
        if not code or credentials is None or _slack_redirect_uri is None:
            return _error(request.query_params.get("error", "authorization was not completed"), 400)
        try:
            token = await exchange_slack_code(credentials, code, _slack_redirect_uri)
            _slack_store.set_token(token)
        except SlackAuthError as error:
            return _error(str(error), 502)
        finally:
            _slack_state = None
            _slack_redirect_uri = None
        return Response(
            "<html><body><p>Slack connected. You can close this window.</p>"
            "<script>window.close()</script></body></html>",
            media_type="text/html",
        )

    async def slack_disconnect(request: Request) -> Response:
        nonlocal _slack_state, _slack_redirect_uri
        if not _is_local_request(request):
            return _error("forbidden", 403)
        token = _slack_store.token()
        if token:
            try:
                await revoke_slack_token(token)
            except SlackAuthError as error:
                return _error(str(error), 502)
        _slack_store.disconnect()
        _slack_state = None
        _slack_redirect_uri = None
        return Response(status_code=204)

    _pending_announcements: list[tuple[RunOrigin, CommunicationsMessage, RunState, asyncio.Event]] = []

    async def concierge_reply(origin: RunOrigin, text: str) -> None:
        await run_notifier.post(origin, CommunicationsMessage(text, mention=origin.author))

    async def concierge_turn_finished(origin: RunOrigin) -> None:
        for pending in list(_pending_announcements):
            ann_origin, ann_msg, ann_state, ready = pending
            if (ann_origin.channel, ann_origin.thread_id) != (origin.channel, origin.thread_id):
                continue
            _pending_announcements.remove(pending)
            try:
                await run_notifier.post(ann_origin, ann_msg, ann_state)
            finally:
                # A failed reply or announcement must not strand an accepted run.
                ready.set()

    async def concierge_create_workorder(
        origin: RunOrigin, repository: str, prompt: str,
    ) -> tuple[str, str]:
        ready = asyncio.Event()
        graph = _mentioned_workflow()
        if graph is None:
            raise RuntimeError("no workflow is configured under `work_orders.workflow`")
        assert surface.runtime is not None
        state = await start_graph_run(
            surface.runtime, graph,
            inputs=resolve_inputs(getattr(graph, "inputs", ()), {}),
            prompt=prompt, repository=repository,
            milestone_id=None, origin=origin,
        )
        link = run_notifier.work_order_link(state)
        _pending_announcements.append((
            origin,
            CommunicationsMessage(
                f"Started a work order on `{repository}`. I will report progress here.",
                (link,) if link else (), mention=origin.author,
            ),
            state, ready,
        ))
        return link.url if link else "", str(state.run_id)

    slack_concierge = SlackConcierge(
        provider=concierge_provider or CodexACPProvider(permissions=tool_permission),
        create_workorder=concierge_create_workorder,
        reply=concierge_reply, default_repository=work_orders.repository,
        turn_finished=concierge_turn_finished,
    )
    _slack_comms = SlackCommunications(_slack_store)
    slack_ingress = SlackIngress(
        slack_concierge, signing_secret=_signing_secret,
        verify_signature=verify_slack_signature, connected=lambda: bool(_slack_store.token()),
        react=_slack_comms.add_reaction,
    )
    # What each delivered comment has led to, for the panel that shows it. The
    # steps are recorded where they happen -- the ingress queues and picks up,
    # the callbacks below forward and answer -- so the panel says what this
    # process did rather than what it was about to try.
    github_activity = GithubActivityLog()

    async def github_reply(origin: RunOrigin, text: str) -> None:
        number, _, review_id = origin.thread_id.partition("/review/")
        await session.capabilities.source_control.add_comment(
            f"https://github.com/{origin.channel.removeprefix('github:')}/pull/{number}",
            text,
            in_reply_to_id=int(review_id) if review_id else None,
        )
        # After the comment lands, so a row reading "replied" means a reader
        # will find the reply on the pull request.
        github_activity.replied(text)

    async def github_run_for_pull_request(repository: str, number: int) -> RunId | None:
        """Which work order opened this pull request, if anything recorded one.

        Read through the binding that owns the provenance table rather than
        through the control surface, which is deliberately forge-agnostic and
        has no business growing a method shaped like a pull request.
        """
        store = getattr(surface.runtime, "store", None)
        if store is None:
            return None
        return await store.run_for_pull_request(repository.lower(), number)

    async def github_start_workorder(
        store: GithubProvenance | None, repository: str, number: int, prompt: str,
        *, replacing: RunId | None,
    ) -> Continuation:
        """Start a work order for a pull request that has no run in flight.

        Deliberately without an origin, unlike the Slack concierge's: the
        commenter is answered on the pull request by the concierge's own fixed
        reply, and a ``github:`` channel is not somewhere the chat provider can
        post -- a run carrying one would send every progress update to a Slack
        channel that does not exist.

        The run claims the pull request as it starts. Provenance is otherwise
        written by opening one, which a run started here never does -- it
        pushes to the pull request the comment arrived on -- so without the
        claim nothing would own it and the next comment would start another
        work order, leaving several agents on one branch and letting anyone who
        can comment create runs without limit. Claiming needs somewhere to
        write, so a deployment whose runtime keeps no provenance starts nothing
        rather than starting what it cannot find again.

        Starting and claiming cannot be one operation -- the run id to claim
        with is the engine's answer to starting -- so the claim decides which
        run keeps the pull request and this cancels the one that did not. Two
        comments arriving together both find nothing in flight and both start,
        and a claim that replaced the earlier one would leave the run it
        displaced alive and unreachable, since every later comment is routed by
        that row. `replacing` is the finished run this pull request was last
        taken on by, if it had one, which is the only claim a start may take
        over: it is what the caller established has stopped working. A claim
        that cannot be written at all leaves nothing behind either -- the run
        is cancelled before the failure is raised, so the redelivery that
        follows starts one run rather than adding one.
        """
        graph = _mentioned_workflow()
        if graph is None:
            raise RuntimeError("no workflow is configured under `work_orders.workflow`")
        if store is None:
            raise RuntimeError(
                "could not start a work order: this runtime records no pull requests"
            )
        runtime = surface.runtime
        assert runtime is not None  # only reached with a runtime in hand
        state = await start_graph_run(
            runtime, graph,
            inputs=resolve_inputs(getattr(graph, "inputs", ()), {}),
            prompt=prompt, repository=repository,
            milestone_id=None,
        )
        url = f"https://github.com/{repository}/pull/{number}"
        try:
            holder = await store.claim_pull_request(
                PullRequestRecord(
                    repository=repository.lower(), number=number, run_id=state.run_id,
                    opened_at=datetime.now(UTC).isoformat(), url=url,
                ),
                replacing=replacing,
            )
        except Exception:
            await _github_cancel_unclaimed(state.run_id)
            raise
        if holder != state.run_id:
            # Lost the race: the pull request is someone else's work order, so
            # the feedback goes there and this run is undone rather than left
            # working a branch nothing can reach.
            await _github_cancel_unclaimed(state.run_id)
            return await github_steer_workorder(holder, prompt)
        link = run_notifier.work_order_link(state)
        return Continuation(
            url=link.url if link else "", run_id=str(state.run_id), started=True,
        )

    async def _github_cancel_unclaimed(run_id: RunId) -> None:
        """Stop a run that did not end up owning its pull request.

        Best effort, and deliberately quiet: whatever is being reported when
        this is called -- the race lost, or the claim write that failed -- is
        the more useful thing to report, and a cancel that fails leaves a run
        visible in the list rather than a silent one.
        """
        runtime = surface.runtime
        if runtime is None:
            return
        try:
            await runtime.cancel(run_id)
        except Exception:
            log.exception("could not cancel unclaimed work order %s", run_id)

    async def github_steer_workorder(run_id: RunId, prompt: str) -> Continuation:
        """Deliver feedback to the work order that holds this pull request."""
        runtime = surface.runtime
        assert runtime is not None  # only reached with a runtime in hand
        snapshot = await runtime.snapshot(run_id)
        if snapshot is None:
            raise RuntimeError("could not reach the work order for this pull request")
        await runtime.steer(run_id, prompt, node_id=_reentry_node(runtime, snapshot))
        state = await session.state_store.load(run_id)
        link = run_notifier.work_order_link(state) if state is not None else None
        return Continuation(url=link.url if link else "", run_id=str(run_id))

    async def github_continue_workorder(origin: RunOrigin, prompt: str) -> Continuation:
        """Steer the work order this pull request already has, or start one.

        Which of the two happens is the host's to decide, not the agent's: it
        turns on what this process recorded when a run took the pull request
        on and on what the graph engine says that run is doing now, neither of
        which a commenter can influence. A pull request nobody is working on --
        opened by hand, or by a run that has since finished or lost its graph
        -- has no execution to steer, and steering one would either raise or
        reach nothing; a comment asking for a change there is a request for
        work, so it gets a work order.
        """
        repository = origin.channel.removeprefix("github:")
        number = int(origin.thread_id.partition("/review/")[0])
        runtime = surface.runtime
        if runtime is None:
            raise RuntimeError("could not reach a work order: graph runtime unavailable")
        # Through the binding that owns the provenance table rather than
        # through the control surface, which is deliberately forge-agnostic and
        # has no business growing a method shaped like a pull request.
        store: GithubProvenance | None = getattr(runtime, "store", None)
        run_id = (
            None if store is None
            else await store.run_for_pull_request(repository.lower(), number)
        )
        snapshot = None
        if run_id is not None:
            try:
                snapshot = await runtime.snapshot(run_id)
            except UnknownGraphError:
                # A saved work order can outlive the graph it was started from.
                snapshot = None
        if snapshot is None or snapshot.status not in STEERABLE_RUN_STATUSES:
            reached = await github_start_workorder(
                store, repository, number, prompt, replacing=run_id,
            )
        else:
            reached = await github_steer_workorder(run_id, prompt)
        # Recorded here rather than in either branch: this is the one place
        # that knows the comment reached a work order at all, and which one --
        # a start that loses the race for the pull request ends up steering
        # somebody else's run, and the panel should name the run that got the
        # feedback rather than the one that was undone.
        github_activity.dispatched(
            reached.run_id, reached.url, started_run=reached.started,
        )
        return reached

    github_concierge = GithubConcierge(
        provider=concierge_provider or CodexACPProvider(permissions=github_tool_permission),
        continue_workorder=github_continue_workorder, reply=github_reply,
    )

    posting_login: dict[str, str] = {}

    async def github_posting_login(repository: str) -> str:
        """The account Engine replies as, asked once per repository.

        Keyed rather than global: a deployment answers one repository today,
        but a resolved login is a property of the credentials *on that forge
        repository*, and an unkeyed cache would quietly hand the first
        repository's answer to the second one's comments.

        ``GITHUB_BOT_LOGIN`` is optional and usually unset, and a token held by
        a machine user posts comments that look like anybody else's: without
        knowing who this process posts as, the concierge answers its own reply
        and then answers that, forever. The credentials themselves are the
        authority on this, so they are asked rather than configured. A failure
        to answer propagates: the turn is retried on redelivery instead of
        replying into a loop this process cannot recognise.
        """
        if repository not in posting_login:
            posting_login[repository] = (
                github_bot_login
                or await session.capabilities.source_control.authenticated_login(
                    f"https://github.com/{repository}"
                )
            )
        return posting_login[repository]

    async def github_concierge_turn(comment: GithubComment) -> None:
        # Issue-driven work orders are not supported. PR conversation comments
        # and inline review replies both belong to an existing work order.
        if not comment.is_pull_request:
            github_activity.ignored("not a pull request")
            return
        # Both lookups reach the forge, and the queue behind this has one
        # worker: a comment that waits here is every later comment waiting too,
        # so they are bounded together rather than left to whatever the
        # configured transport happens to bound. Timing out raises, which the
        # ingress treats like any other failure -- the comment is forgotten and
        # can be redelivered -- so a slow forge costs a retry, not the queue.
        async with asyncio.timeout(GITHUB_AUTHORIZATION_TIMEOUT_SECONDS):
            if comment.author.lower() == (
                await github_posting_login(comment.repository)
            ).lower():
                # GitHub logins are case-insensitive, so the comparison is too.
                github_activity.ignored("posted by Engine itself")
                return
            # Before the comment becomes a prompt, not after the model has
            # acted on one. A comment is untrusted text and the agent that
            # reads it can read the host it runs on, so whoever writes one is
            # choosing what this process reads and what it says back in public.
            # `author_association` does not bound that -- a COLLABORATOR may
            # hold read access alone -- and gating the outbound tool alone
            # would still have run the turn. Write access is the line: it is
            # already the authority to change this repository, so it is no
            # escalation to reach the agent working on it.
            may_write = await session.capabilities.source_control.can_write_repository(
                f"https://github.com/{comment.repository}/pull/{comment.number}",
                comment.author,
            )
        if not may_write:
            # Ignored rather than answered, like the association filter above:
            # a refusal posted back is both noise on the pull request and a way
            # to make this process talk to somebody it will not act for.
            log.info(
                "ignored a GitHub comment from %s, who cannot write to %s",
                comment.author, comment.repository,
            )
            github_activity.ignored(
                f"{comment.author} cannot write to {comment.repository}"
            )
            return
        thread_id = str(comment.number)
        if comment.event == "pull_request_review_comment":
            thread_id += f"/review/{comment.in_reply_to_id or comment.comment_id}"
        await github_concierge.handle(FeedbackRequest(
            origin=RunOrigin(
                channel=f"github:{comment.repository}", thread_id=thread_id,
                author=comment.author,
            ),
            text=comment.body, comment_id=comment.comment_id,
        ))

    github_ingress = GithubIngress(
        webhook_secret=github_webhook_secret,
        repository=github_repository,
        self_login=lambda: github_bot_login,
        handle=github_comment_handler or github_concierge_turn,
        activity=github_activity,
    )

    async def run_github_comments(request: Request) -> JSONResponse:
        """The GitHub comments left on this WorkOrder's pull request.

        Scoped by path rather than filtered by query because a comment only
        means anything next to the work it steered: read on its own it is a
        line from a conversation with no subject. Hanging it under the run
        also keeps the one listing of every comment this process ever saw --
        including comments about work that is none of this WorkOrder's
        business -- off the API entirely.

        The WorkOrder each comment belongs to is resolved here rather than
        remembered with the comment: a pull request's owner is written down
        when it is opened, which can be after a comment on it was recorded, so
        joining at read time is what lets a row reach the right page at all.
        """
        entries = github_activity.recent()
        owners: dict[tuple[str, int], str] = {}
        for entry in entries:
            pull_request = (entry.repository.lower(), entry.number)
            if pull_request not in owners:
                owner = await github_run_for_pull_request(*pull_request)
                owners[pull_request] = str(owner) if owner is not None else ""
        return JSONResponse(activity_json(
            entries,
            owners=owners,
            repository=github_repository,
            # Both halves, because either one missing is a webhook that will
            # never deliver anything here -- and a panel that stayed empty
            # without saying so is the confusion this is meant to end.
            configured=bool(github_repository and github_webhook_secret()),
            queued=github_ingress.queued,
            working=github_concierge.busy,
            sessions=github_concierge.sessions,
            run_id=request.path_params["run_id"],
        ))

    def _mentioned_workflow() -> GraphWorkflow | None:
        """Which workflow a mention runs: the configured one, or the only one.

        Answered from the graphs actually on offer rather than from the
        catalog, because a mention that resolved to a workflow this process
        could not start would be accepted and then go nowhere.
        """
        offered = offered_graphs()
        if work_orders.workflow:
            return offered.get(work_orders.workflow)
        return next(iter(offered.values())) if len(offered) == 1 else None

    # --- runner utilization ---------------------------------------------------

    _utilization = utilization or UtilizationService()

    async def read_utilization(_request: Request) -> JSONResponse:
        """What was true the last time anybody looked, answered without looking.

        Deliberately offline: this is what the page draws while the scrape
        below is still in flight, so it must not wait on the same providers.
        """
        return JSONResponse(utilization_json(_utilization.cached()))

    async def refresh_utilization(request: Request) -> Response:
        # Reads the tokens the runners signed in with, so it is held to the same
        # origin check as the other endpoints that touch a stored credential.
        if not _is_local_request(request):
            return _error("forbidden", 403)
        readings = await _utilization.refresh(tuple(runners))
        return JSONResponse(utilization_json(readings))

    github_login = GitHubLogin(github_login_config)
    routes = [
        *github_login.routes(),
        Route("/api/config", config),
        Route("/api/github/status", github_status),
        Route("/api/source-control/status", source_control_status),
        Route("/api/source-control/provider", source_control_provider_status),
        Route(
            "/api/source-control/provider",
            source_control_provider_status,
            methods=["GET"],
        ),
        Route(
            "/api/source-control/provider",
            set_source_control_provider,
            methods=["POST"],
        ),
        Route("/api/github/client-id", github_get_client_id),
        Route("/api/github/client-id", github_set_client_id, methods=["POST"]),
        Route("/api/github/connect", github_connect, methods=["POST"]),
        Route("/api/github/connect/poll", github_connect_poll, methods=["POST"]),
        Route("/api/github/disconnect", github_disconnect, methods=["POST"]),
        Route("/api/gitlab/status", gitlab_status),
        Route("/api/gitlab/client-id", gitlab_set_client_id, methods=["POST"]),
        Route("/api/gitlab/connect", gitlab_connect, methods=["POST"]),
        Route("/api/gitlab/connect/poll", gitlab_connect_poll, methods=["POST"]),
        Route("/api/gitlab/disconnect", gitlab_disconnect, methods=["POST"]),
        Route("/api/slack/status", slack_status),
        Route("/api/slack/credentials", slack_set_credentials, methods=["POST"]),
        Route("/api/slack/connect", slack_connect, methods=["POST"]),
        Route("/api/slack/callback", slack_callback, name="slack_callback"),
        Route("/api/slack/disconnect", slack_disconnect, methods=["POST"]),
        Route("/api/slack/events", slack_ingress.webhook, methods=["POST"]),
        Route("/api/utilization", read_utilization),
        Route("/api/utilization/refresh", refresh_utilization, methods=["POST"]),
        Route("/api/projects", list_projects),
        Route("/api/projects", create_project, methods=["POST"]),
        Route(
            "/api/projects/{project_id}/archive",
            archive_project,
            methods=["POST"],
            name="archive_project",
        ),
        Route(
            "/api/projects/{project_id}/unarchive",
            archive_project,
            methods=["POST"],
            name="unarchive_project",
        ),
        Route("/api/projects/{project_id}/milestones", list_project_milestones),
        Route(
            "/api/projects/{project_id}/milestones/{milestone_id}/scope",
            scope_milestone,
            methods=["POST"],
        ),
        Route("/api/runs", list_runs),
        Route("/api/runs", create_run, methods=["POST"]),
        Route("/api/runs/{run_id}", get_run),
        Route("/api/runs/{run_id}/start", start_scheduled_run, methods=["POST"]),
        Route("/api/runs/{run_id}", delete_run, methods=["DELETE"]),
        Route("/api/runs/{run_id}/graph-events", graph_run_events),
        Route("/api/runs/{run_id}/github-comments", run_github_comments),
        # The graph half of the runs above, served by the engine that runs
        # them rather than by this file.
        Mount(GRAPH_PREFIX, app=graph_surface),
        Route("/api/threads", list_threads),
        Route("/api/threads", create_thread, methods=["POST"]),
        Route("/api/threads/{thread_id}", get_thread),
        Route("/api/threads/{thread_id}", update_thread, methods=["PATCH"]),
        Route("/api/threads/{thread_id}", delete_thread, methods=["DELETE"]),
        Route(
            "/api/threads/{thread_id}/archive",
            archive_thread,
            methods=["POST"],
            name="archive",
        ),
        Route(
            "/api/threads/{thread_id}/unarchive",
            archive_thread,
            methods=["POST"],
            name="unarchive",
        ),
        Route("/api/threads/{thread_id}/messages", messages),
        Route("/api/threads/{thread_id}/approval-events", approval_events),
        Route(
            "/api/threads/{thread_id}/workspace",
            attach_workspace,
            methods=["POST"],
        ),
        Route(
            "/api/threads/{thread_id}/workspace",
            detach_workspace,
            methods=["DELETE"],
        ),
        Route("/api/threads/{thread_id}/title", title_thread, methods=["POST"]),
        Route("/api/threads/{thread_id}/runs", run_thread, methods=["POST"]),
        Route("/api/threads/{thread_id}/runs/current", resume_run),
        Route("/api/threads/{thread_id}/runs/current", cancel_run, methods=["DELETE"]),
        Route(
            "/api/threads/{thread_id}/runs/current/approvals/{approval_id}",
            decide_approval,
            methods=["POST"],
        ),
    ]
    routes.append(Route("/api/github/events", github_ingress.webhook, methods=["POST"]))
    if static_directory is not None and (static_directory / "index.html").is_file():

        async def spa_page(_request: Request) -> Response:
            return FileResponse(
                static_directory / "index.html",
                headers={"cache-control": "no-cache"},
            )

        routes.extend(
            [
                Route("/login", spa_page),
                Route("/runs", spa_page),
                Route("/runs/new", spa_page),
                Route("/runs/{run_id}/conversations/{thread_id}", spa_page),
                Route("/runs/{run_id}", spa_page),
                Route("/conversations", spa_page),
                Route("/conversations/{thread_id}", spa_page),
                Route("/plan", spa_page),
                Route("/utilization", spa_page),
                Route("/projects/{project_id}/milestones", spa_page),
                Route("/projects/{project_id}/milestones/{milestone_id}", spa_page),
                Route(
                    "/projects/{project_id}/milestones/{milestone_id}/scope",
                    spa_page,
                ),
                Route(
                    "/projects/{project_id}/milestones/{milestone_id}/tasks/new",
                    spa_page,
                ),
            ]
        )
        routes.append(Mount("/", BuiltClient(directory=static_directory, html=True)))
    else:
        routes.append(Route("/", _missing_frontend))
    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.thread_service = service
    app.state.milestone_scoper = milestone_scoper
    app.state.slack_ingress = slack_ingress
    app.state.github_ingress = github_ingress
    # Enforce session auth on API routes when GitHub login is configured.
    app = github_login.middleware(app)
    return app


def _with_workspace(thread: ChatThread, state: WorkspaceState | None) -> ChatThread:
    """Fold a provider's answer into the thread the UI is shown."""
    thread.workspace_id = state.workspace_id if state is not None else None
    thread.workspace_ref = state.ref if state is not None else None
    thread.workspace_root = state.root_path if state is not None else None
    return thread


def _thread_json(thread: ChatThread) -> dict[str, object]:
    result: dict[str, object] = {
        "id": str(thread.instance_id),
        "title": thread.title,
        "archived": thread.archived,
        "agentId": str(thread.agent_id),
        "runner": thread.runner,
        # Present but detached is a state of its own: the work is still there,
        # on the ref, and attaching brings a checkout back to it.
        "workspaceAttached": thread.workspace_root is not None,
    }
    if thread.workspace_root is not None:
        result["workspaceRoot"] = thread.workspace_root
    if thread.workspace_ref is not None:
        result["workspaceRef"] = thread.workspace_ref
    return result


def _project_json(
    project: Project,
    conversations: Container[AgentInstanceId],
    *,
    milestones: int | None = None,
) -> dict[str, object]:
    """Render a project, linked to its plan when that conversation is open.

    `conversations` is required rather than defaulted: a caller that forgot it
    would quietly emit the linkless rows this link exists to replace.

    `milestones` is how many the project has, for the callers that counted
    them. Left out rather than reported as none by the ones that did not: a
    zero here is a project with no plan, which is a thing the rail acts on.
    """

    result: dict[str, object] = {
        "projectId": str(project.project_id),
        "name": project.name,
        "archived": project.archived,
    }
    if milestones is not None:
        result["milestoneCount"] = milestones
    instance_id = instance_id_for_project(project.project_id)
    if instance_id is not None and instance_id in conversations:
        result["conversationUrl"] = f"/conversations/{quote(str(instance_id), safe='')}"
    return result


def _milestone_json(milestone: Milestone) -> dict[str, object]:
    return {
        "milestoneId": str(milestone.milestone_id),
        "name": milestone.name,
        "description": milestone.description,
        "dependencies": [str(dependency) for dependency in milestone.dependencies],
    }


def _workorder_for_run(run: RunState, milestone_id: MilestoneId) -> WorkOrder:
    """Present scheduled and active milestone work to the scoper."""
    if run.phase is RunPhase.SCHEDULED:
        status = WorkOrderStatus.SCHEDULED
    elif run.phase is RunPhase.SUCCEEDED:
        status = WorkOrderStatus.COMPLETE
    elif run.phase is RunPhase.FAILED:
        status = WorkOrderStatus.CANCELLED
    elif run.phase is RunPhase.PENDING:
        status = WorkOrderStatus.PENDING
    else:
        status = WorkOrderStatus.IN_PROGRESS
    return WorkOrder(
        workorder_id=WorkOrderId(str(run.run_id)),
        spec=WorkOrderSpec(
            milestone_id=milestone_id,
            name=run.name or str(run.task_id),
            objective=run.prompt,
        ),
        status=status,
    )


def _workorder_spec_json(spec: WorkOrderSpec) -> dict[str, object]:
    return {
        "milestoneId": spec.milestone_id,
        "name": spec.name,
        "objective": spec.objective,
        "evidenceRequirements": list(spec.evidence_requirements),
        "dependencies": list(spec.dependencies),
    }


def _scoping_plan_json(plan: ScopingPlan) -> dict[str, object]:
    """Translate the domain result without flattening its three operations."""
    return {
        "create": [_workorder_spec_json(spec) for spec in plan.create],
        "cancel": list(plan.cancel),
        "supersede": [
            {
                "workorderId": item.workorder_id,
                "replacements": [
                    _workorder_spec_json(spec) for spec in item.replacements
                ],
            }
            for item in plan.supersede
        ],
        "reasons": list(plan.reasons),
    }


def _run_json(run: WorkflowRunView, *, listing: bool = False) -> dict[str, object]:
    """One WorkOrder, as a client is shown it.

    A listing leaves out the prose: the task prompt and a failure's reason.
    Every screen polls `/api/runs` once a second to keep the rail current, so
    what that list carries is what every screen pays for, on a payload that
    grows with every run ever started. The pages that draw the prose read the
    one run they are about from `/api/runs/{run_id}`.
    """
    result: dict[str, object] = {
        "runId": str(run.run_id),
        "name": run.name,
        "workflowId": run.workflow_id,
        "workflowName": run.workflow_name,
        "taskId": run.task_id,
        "milestoneId": str(run.milestone_id) if run.milestone_id else None,
        "repository": run.repository,
        "repositoryContext": {"repository": run.repository},
        "phase": run.phase,
        "terminalOutcome": run.terminal_outcome,
    }
    if listing:
        return result
    result["taskPrompt"] = run.task_prompt
    result["failureReason"] = run.failure_reason
    return result


def _approval_json(approval: ApprovalRecord) -> dict[str, object]:
    """One complete request, as the client is shown it.

    Whole rather than incremental, like the content snapshots beside it: a
    client that reconnected mid-pause has no way to reconstruct a request from
    the parts of it that were emitted before it arrived.
    """
    result = {
        "id": str(approval.approval_id),
        "status": approval.status.value,
        "kind": approval.kind.value,
        "reason": approval.reason,
        "command": approval.command,
        "cwd": approval.cwd,
        "toolName": approval.tool_name,
        # The call this was asked about, so the client can show the request
        # beside it rather than collecting every request at the end of a turn.
        "toolCallId": approval.tool_call_id,
        "arguments": approval.arguments,
        "allowedDecisions": [decision.value for decision in approval.allowed_decisions],
        "decision": approval.decision.value if approval.decision else None,
        # Who decided, so the client can tell an answer the user gave from one
        # a grant gave on their behalf. A request nobody was shown still has to
        # read as something that happened, not as something that was skipped.
        "decisionSource": (
            approval.decision_source.value if approval.decision_source else None
        ),
    }
    if approval.questions is not None:
        result["questions"] = _json_value(approval.questions, [])
    if approval.answers is not None:
        result["answers"] = _json_value(approval.answers, None)
    return result


def _json_value(value: str | None, default: object) -> object:
    if value is None:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def _messages_json(messages: tuple[Message, ...]) -> list[dict[str, object]]:
    """Group the engine's turn transcript into assistant-ui messages."""
    result: list[dict[str, object]] = []
    assistant_content: list[dict[str, object]] = []
    assistant_id = ""
    tool_calls: dict[str, dict[str, object]] = {}

    def flush_assistant() -> None:
        nonlocal assistant_content, assistant_id
        if assistant_content:
            result.append(
                {
                    "id": assistant_id or f"assistant-{len(result)}",
                    "role": Role.ASSISTANT.value,
                    "content": assistant_content,
                }
            )
        assistant_content = []
        assistant_id = ""

    for index, message in enumerate(messages):
        if message.role is Role.USER:
            flush_assistant()
            if message.content:
                result.append(
                    {
                        "id": str(message.message_id or f"user-{index}"),
                        "role": Role.USER.value,
                        "content": [{"type": "text", "text": message.content}],
                    }
                )
            continue
        if not assistant_id and message.message_id:
            assistant_id = str(message.message_id)
        _merge_message(assistant_content, message, tool_calls)
    flush_assistant()
    return result


def _tool_call_ids(messages: Iterable[Message]) -> set[str]:
    """Every provider call id already present in a transcript."""
    return {call.call_id for message in messages for call in message.tool_calls}


def _merge_message(
    content: list[dict[str, object]],
    message: Message,
    tool_calls: dict[str, dict[str, object]] | None = None,
) -> bool:
    """Fold one engine message into one assistant-ui assistant response."""
    if tool_calls is None:
        tool_calls = {
            str(part["toolCallId"]): part
            for part in content
            if part.get("type") == "tool-call" and "toolCallId" in part
        }
    changed = False
    if message.role is Role.ASSISTANT:
        if message.content:
            content.append({"type": "text", "text": message.content})
            changed = True
        for call in message.tool_calls:
            # Provider streams and resumed sessions can replay the same item.
            # It is one call semantically, and assistant-ui requires it to be
            # one resource structurally, so retain the first occurrence.
            if call.call_id in tool_calls:
                continue
            try:
                arguments = json.loads(call.arguments)
            except json.JSONDecodeError:
                arguments = {}
            if not isinstance(arguments, dict):
                arguments = {"value": arguments}
            part: dict[str, object] = {
                "type": "tool-call",
                "toolCallId": call.call_id,
                "toolName": call.name,
                "args": arguments,
                "argsText": call.arguments,
            }
            content.append(part)
            tool_calls[call.call_id] = part
            clarification = _clarification_context(call.name, arguments)
            if clarification:
                content.append({"type": "text", "text": clarification})
            changed = True
    elif message.role is Role.TOOL and message.tool_call_id:
        part = tool_calls.get(message.tool_call_id)
        if part is not None:
            part["result"] = message.content
            changed = any(candidate is part for candidate in content)
    return changed


_CLARIFICATION_TOOLS = frozenset(
    {
        "askuserquestion",
        "escalate",
        "escalatetohuman",
        "requestclarification",
        "requesthumanreview",
        "requestuserinput",
    }
)


def _clarification_context(tool_name: str, arguments: object) -> str | None:
    """Extract the question text from a provider's clarification tool call."""

    leaf_name = tool_name.rsplit("__", 1)[-1].rsplit(".", 1)[-1]
    normalized = "".join(
        character for character in leaf_name.lower() if character.isalnum()
    )
    if normalized not in _CLARIFICATION_TOOLS or not isinstance(arguments, dict):
        return None

    questions = arguments.get("questions")
    candidates = questions if isinstance(questions, list) else (arguments,)
    context: list[str] = []
    for candidate in candidates:
        if isinstance(candidate, str):
            text = candidate.strip()
        elif isinstance(candidate, dict):
            text = next(
                (
                    value.strip()
                    for key in ("question", "prompt", "message")
                    if isinstance((value := candidate.get(key)), str) and value.strip()
                ),
                "",
            )
        else:
            text = ""
        if text and text not in context:
            context.append(text)
    return "\n\n".join(context) or None


_TITLE_PROMPT = (
    "Name this chat based on the conversation above. Reply with only a concise "
    "title of at most eight words, with no quotes or ending punctuation."
)


def _clean_title(value: str) -> str:
    first_line = value.strip().splitlines()[0] if value.strip() else ""
    return first_line.strip(" \t\"'`).:;!?")[:80]


async def _json_body(request: Request) -> dict[str, object]:
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return {}
    return body if isinstance(body, dict) else {}


def _required_string(body: dict[str, object], name: str) -> str:
    value = body.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _optional_string(body: dict[str, object], name: str) -> str | None:
    value = body.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _thread_id(request: Request) -> AgentInstanceId:
    return AgentInstanceId(request.path_params["thread_id"])


def _new_agent_run_id() -> AgentRunId:
    return AgentRunId(f"ar-{uuid4().hex[:12]}")


def _json_line(value: dict[str, object]) -> bytes:
    return (json.dumps(value, separators=(",", ":")) + "\n").encode()


def _server_event(value: dict[str, object]) -> bytes:
    return f"data:{json.dumps(value, separators=(',', ':'))}\n\n".encode()


def _error(message: str, status_code: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status_code)


async def _missing_frontend(_request: Request) -> Response:
    return Response(
        "The assistant-ui client has not been built. Run `npm --prefix apps/web run build`.",
        status_code=503,
        media_type="text/plain",
    )


__all__ = ["ChatThread", "ThreadService", "create_app"]
