"""HTTP surface for the assistant-ui client.

The engine owns conversations; assistant-ui owns their presentation.  This
module translates between those two vocabularies and keeps the small amount of
thread metadata that is UI-specific (title, archive status, selected runner).

Runs are streamed as newline-delimited JSON.  Their tasks are owned by the
service rather than by one response, so a refreshed browser can reconnect.
A lock per thread prevents two turns from reading the same stale transcript.

A run that pauses for approval keeps that pause in the same place: the request
is a snapshot on the `ActiveRun`, replayed to whoever reconnects, and the
decision arrives as its own request rather than on the stream that showed it.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator, Awaitable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import uuid4

from engine.domain import (
    AgentId,
    AgentInstanceId,
    AgentRunId,
    AgentRunStatus,
    ApprovalDecision,
    ApprovalId,
    ApprovalRecord,
    IMPLEMENTATION_REVIEW_WORKFLOW_ID,
    Message,
    Role,
    RunId,
    RunRequested,
    RunState,
    StepId,
    TaskId,
    WorkflowId,
    WorkspaceId,
)
from engine.ports import (
    AgentRunner,
    ApprovalHandler,
    InteractiveAgentRunner,
    WorkspaceState,
)
from engine.runtime import (
    AgentSession,
    ApprovalBroker,
    ApprovalDecisionNotAllowedError,
    ApprovalNotPendingError,
    RunReader,
    UnknownApprovalError,
    WorkflowExecutor,
    WorkflowRunView,
)
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.types import Scope


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
    workflow_run_id: RunId | None = None
    workflow_step_id: StepId | None = None


class ActiveRun:
    """One agent turn whose lifetime is independent of an HTTP connection.

    Subscribers receive complete content snapshots, so a browser that refreshes
    can reconnect without needing to know which individual events it missed. An
    approval is the same idea and for the same reason: the turn is paused on a
    question, and a subscriber that arrives after it was asked has to be told
    the question rather than left watching a stream that has gone quiet.
    """

    def __init__(self, agent_run_id: AgentRunId) -> None:
        self.agent_run_id = agent_run_id
        self.content: list[dict[str, object]] = []
        self.approval: dict[str, object] | None = None
        """The latest approval snapshot, pending or resolved. None until asked."""
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
        approval: dict[str, object] | None = None
        while True:
            async with self._changed:
                await self._changed.wait_for(
                    lambda: self._revision > revision or self.done
                )
                revision = self._revision
                content = [dict(part) for part in self.content]
                pending = dict(self.approval) if self.approval is not None else None
                error = self.error
                done = self.done

            if pending != approval:
                # Whole snapshots, including the resolved one: a client that
                # missed the decision would otherwise go on showing a prompt
                # for a request that has already been answered. Emitted before
                # the terminal events so the last thing said about a request is
                # never lost to the run ending in the same breath.
                approval = pending
                yield _json_line({"type": "approval", "approval": pending})
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
        if not _merge_message(self.content, message):
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
            self.approval = snapshot
            self._revision += 1
            self._changed.notify_all()

    def note_approval(self, approval: ApprovalRecord) -> None:
        """Update the snapshot without waking anyone, for a run that is ending.

        Synchronous on purpose. The wake that matters is the one the run's own
        ending sends a moment later, and awaiting a lock here would yield the
        event loop back to the very turn being torn down.
        """
        self.approval = _approval_json(approval)

    async def _finish(self) -> None:
        async with self._changed:
            self.done = True
            self._revision += 1
            self._changed.notify_all()


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
        self, session: AgentSession, runners: Mapping[str, AgentRunner]
    ) -> None:
        self.session = session
        self.approvals = ApprovalBroker(session.state_store)
        """Public alongside `session`: the same durable boundary, for pauses."""
        self._runners = runners
        self._threads: dict[AgentInstanceId, ChatThread] = {}
        self._locks: dict[AgentInstanceId, asyncio.Lock] = {}
        self._active_runs: dict[AgentInstanceId, ActiveRun] = {}
        self._restored = False
        self._restore_lock = asyncio.Lock()

    async def list(self) -> tuple[ChatThread, ...]:
        await self._restore()
        return tuple(
            thread
            for thread in reversed(self._threads.values())
            if thread.workflow_run_id is None
        )

    async def get(self, instance_id: AgentInstanceId) -> ChatThread | None:
        await self._restore()
        thread = self._threads.get(instance_id)
        if thread is not None:
            return thread
        # Workflow workers may materialize a step after this web process has
        # restored its initial registry. Resolve direct conversation links from
        # the durable store instead of requiring a server restart.
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
            workflow_run_id=instance.workflow_run_id,
            workflow_step_id=instance.workflow_step_id,
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
        return _with_workspace(thread, state)

    async def detach_workspace(self, instance_id: AgentInstanceId) -> ChatThread:
        """Release this chat's checkout, keeping its work on the branch."""
        thread = await self._require_idle(instance_id)
        async with self._locks[instance_id]:
            state = await self.session.detach_workspace(instance_id)
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
        await self._require_somewhere_to_run(instance_id)
        initial_message_count = len(await self.session.history(instance_id))
        current = self.active_run(instance_id)
        if current is not None:
            raise RuntimeError("this chat already has a run in progress")

        observed: asyncio.Queue[Message] = asyncio.Queue()
        # Named before it starts, because the approvals it raises are brokered
        # against this run and a decision has to be able to name it too.
        agent_run_id = _new_agent_run_id()
        selected_runner = runner or thread.runner
        run = ActiveRun(agent_run_id)
        self._active_runs[instance_id] = run
        on_approval = None
        if isinstance(self._runners.get(selected_runner), InteractiveAgentRunner):
            on_approval = self.approvals.handler(
                agent_run_id=agent_run_id,
                instance_id=instance_id,
                runner=selected_runner,
                present=run.present_approval,
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
                        message = await asyncio.wait_for(observed.get(), timeout=0.1)
                    except asyncio.TimeoutError:
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
                    # leaves one here. The last of them is what the run is
                    # still showing, so it is the one that has to stop saying
                    # "pending", whoever resolved it.
                    await self.approvals.interrupt_run(agent_run_id)
                    asked = await self.session.state_store.list_approvals(
                        agent_run_id=agent_run_id
                    )
                    if asked:
                        run.note_approval(asked[-1])

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
        self, instance_id: AgentInstanceId, approval_id: ApprovalId, decision: str
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
            agent_run_id=run.agent_run_id if run is not None else None,
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
        if thread.title != "New chat":
            return thread.title
        selected_runner = runner or thread.runner
        if selected_runner not in self.session.runners:
            raise ValueError(f"unknown runner {selected_runner!r}")
        async with self._locks[instance_id]:
            if thread.title != "New chat":
                return thread.title
            history = await self.session.history(instance_id)
            title_context = (
                (*history, Message.user(opening_text)) if opening_text else history
            )
            turn = await self._runners[selected_runner].run_turn(
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
            thread.instance_id, thread.title, thread.archived, thread.runner
        )

    async def _require(self, instance_id: AgentInstanceId) -> ChatThread:
        thread = await self.get(instance_id)
        if thread is None:
            raise KeyError(f"no chat thread {instance_id!r}")
        return thread

    async def _require_somewhere_to_run(self, instance_id: AgentInstanceId) -> None:
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
            return _with_workspace(thread, await self.session.workspace(thread.instance_id))
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
                    workflow_run_id=instance.workflow_run_id,
                    workflow_step_id=instance.workflow_step_id,
                )
                self._threads[instance.instance_id] = await self._sync_workspace(thread)
                self._locks[instance.instance_id] = asyncio.Lock()
            # A CLI subprocess does not survive the server that spawned it, so
            # a request still marked pending here was asked by a process that
            # no longer exists and can never be answered.
            await self.approvals.interrupt_orphans()
            self._restored = True


def create_app(
    session: AgentSession,
    runners: Mapping[str, AgentRunner],
    static_directory: Path | None = None,
    *,
    workflow_runners: Mapping[str, AgentRunner] | None = None,
) -> Starlette:
    """Build the web application around already-composed capabilities."""
    service = ThreadService(session, runners)
    run_reader = RunReader(session.state_store)
    workflow_executor = WorkflowExecutor(
        session.capabilities,
        workflow_runners or runners,
    )
    workflow_tasks: dict[RunId, asyncio.Task[None]] = {}

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
                "defaultRunner": session.default_runner,
                "workflowRunners": list(workflow_executor.runners),
                "defaultWorkflowRunner": workflow_executor.default_runner,
            }
        )

    async def list_threads(_request: Request) -> JSONResponse:
        return JSONResponse({"threads": [_thread_json(t) for t in await service.list()]})

    async def list_runs(_request: Request) -> JSONResponse:
        return JSONResponse(
            {"runs": [_run_json(run) for run in await run_reader.list()]}
        )

    async def create_run(request: Request) -> JSONResponse:
        """Persist a workflow request and start its supported local execution."""
        body = await _json_body(request)
        try:
            prompt = _required_string(body, "prompt")
            repository = _required_string(body, "repository")
            workflow_id = WorkflowId(_required_string(body, "workflowId"))
        except ValueError as error:
            return _error(str(error), 400)
        if workflow_id != IMPLEMENTATION_REVIEW_WORKFLOW_ID:
            return _error(f"unknown workflow definition: {workflow_id}", 400)
        runner_name = str(body.get("runner") or workflow_executor.default_runner)
        if runner_name not in workflow_executor.runners:
            return _error(f"unknown workflow runner: {runner_name}", 400)

        run_id = RunId(f"run-{uuid4().hex[:12]}")
        task_id = TaskId(f"task-{uuid4().hex[:12]}")
        event = RunRequested(
            run_id=run_id,
            task_id=task_id,
            prompt=prompt,
            repository=repository,
            workflow_id=workflow_id,
        )
        state = RunState(
            run_id=run_id,
            task_id=task_id,
            workflow_id=workflow_id,
            prompt=prompt,
            repository=repository,
        )
        await session.state_store.save(state)
        await session.state_store.append_events(run_id, (event,))
        task = asyncio.create_task(
            workflow_executor.advance_to_review(event, runner_name)
        )
        workflow_tasks[run_id] = task
        task.add_done_callback(lambda _task: workflow_tasks.pop(run_id, None))
        run = await run_reader.get(run_id)
        assert run is not None
        return JSONResponse(_run_json(run), status_code=201)

    async def get_run(request: Request) -> JSONResponse:
        run = await run_reader.get(RunId(request.path_params["run_id"]))
        if run is None:
            return _error("run not found", 404)
        return JSONResponse(_run_json(run))

    async def create_thread(request: Request) -> JSONResponse:
        body = await _json_body(request)
        try:
            thread = await service.create(
                AgentId(_required_string(body, "agentId")),
                _required_string(body, "runner"),
            )
        except (KeyError, ValueError) as error:
            return _error(str(error), 400)
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
        try:
            history = await service.history(instance_id)
        except KeyError:
            return _error("thread not found", 404)
        active = service.active_run(instance_id)
        return JSONResponse(
            {
                "messages": _messages_json(history),
                # A complete assistant transcript can become durable just
                # before ActiveRun flips to done. In that window replaying it
                # would duplicate the assistant message in the client.
                "unstable_resume": active is not None
                and bool(history)
                and history[-1].role is Role.USER,
            }
        )

    async def title_thread(request: Request) -> JSONResponse:
        instance_id = _thread_id(request)
        if await service.get(instance_id) is None:
            return _error("thread not found", 404)
        body = await _json_body(request)
        opening_text = str(body["text"]).strip() if body.get("text") else None
        runner = str(body["runner"]) if body.get("runner") else None
        try:
            title = await service.generate_title(instance_id, opening_text, runner)
        except ValueError as error:
            return _error(str(error), 400)
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
        if await service.get(instance_id) is None:
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
        except RuntimeError as error:
            return _error(str(error), 409)
        return StreamingResponse(run.stream(), media_type="application/x-ndjson")

    async def resume_run(request: Request) -> Response:
        instance_id = _thread_id(request)
        if await service.get(instance_id) is None:
            return _error("thread not found", 404)
        # Keep a completed snapshot available for the small race where history
        # observed an active run immediately before it finished.
        run = service.latest_run(instance_id)
        if run is None:
            return Response(status_code=204)
        return StreamingResponse(run.stream(), media_type="application/x-ndjson")

    async def cancel_run(request: Request) -> Response:
        instance_id = _thread_id(request)
        if await service.get(instance_id) is None:
            return _error("thread not found", 404)
        await service.stop_run(instance_id)
        return Response(status_code=204)

    async def decide_approval(request: Request) -> Response:
        instance_id = _thread_id(request)
        if await service.get(instance_id) is None:
            return _error("thread not found", 404)
        body = await _json_body(request)
        try:
            decision = _required_string(body, "decision")
        except ValueError as error:
            return _error(str(error), 400)
        try:
            approval = await service.decide_approval(
                instance_id, ApprovalId(request.path_params["approval_id"]), decision
            )
        except UnknownApprovalError as error:
            return _error(str(error), 404)
        except ApprovalDecisionNotAllowedError as error:
            return _error(str(error), 400)
        except ApprovalNotPendingError as error:
            # The request outlived whatever was waiting for it. Not the
            # client's mistake to fix by retrying, so not a 400.
            return _error(str(error), 409)
        return JSONResponse({"approval": _approval_json(approval)})

    routes = [
        Route("/api/config", config),
        Route("/api/runs", list_runs),
        Route("/api/runs", create_run, methods=["POST"]),
        Route("/api/runs/{run_id}", get_run),
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
    if static_directory is not None and (static_directory / "index.html").is_file():
        async def spa_page(_request: Request) -> Response:
            return FileResponse(
                static_directory / "index.html",
                headers={"cache-control": "no-cache"},
            )

        routes.extend(
            [
                Route("/runs", spa_page),
                Route("/runs/new", spa_page),
                Route("/runs/{run_id}", spa_page),
                Route("/conversations", spa_page),
                Route("/conversations/{thread_id}", spa_page),
            ]
        )
        routes.append(Mount("/", BuiltClient(directory=static_directory, html=True)))
    else:
        routes.append(Route("/", _missing_frontend))
    app = Starlette(routes=routes)
    app.state.thread_service = service
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
    if thread.workflow_run_id is not None:
        result["workflowRunId"] = str(thread.workflow_run_id)
    if thread.workflow_step_id is not None:
        result["workflowStepId"] = str(thread.workflow_step_id)
    return result


def _run_json(run: WorkflowRunView) -> dict[str, object]:
    result: dict[str, object] = {
        "runId": str(run.run_id),
        "workflowId": run.workflow_id,
        "workflowName": run.workflow_name,
        "workflowVersion": run.workflow_version,
        "taskId": run.task_id,
        "taskPrompt": run.task_prompt,
        "repository": run.repository,
        "repositoryContext": {"repository": run.repository},
        "phase": run.phase,
        "currentStepId": str(run.current_step_id) if run.current_step_id else None,
        "terminalOutcome": run.terminal_outcome,
        "failureReason": run.failure_reason,
        "steps": [
            {
                "stepId": str(step.step_id),
                "name": step.name,
                "kind": step.kind,
                "status": step.status,
                "outcome": step.outcome,
                "summary": step.summary,
                "outputs": [
                    {"name": output.name, "value": output.value}
                    for output in step.outputs
                ],
                "changesRequested": step.changes_requested,
                "agentId": str(step.agent_id) if step.agent_id else None,
                "agentInstanceId": (
                    str(step.agent_instance_id) if step.agent_instance_id else None
                ),
                "agentRunId": str(step.agent_run_id) if step.agent_run_id else None,
                "mcpRequestId": step.mcp_request_id,
                "conversationId": (
                    str(step.conversation_id) if step.conversation_id else None
                ),
                "conversationUrl": (
                    f"/conversations/{step.agent_instance_id}"
                    if step.agent_instance_id
                    else None
                ),
            }
            for step in run.steps
        ],
    }
    if run.pending_human_review is not None:
        result["pendingHumanReview"] = {
            "stepId": str(run.pending_human_review.step_id),
            "title": run.pending_human_review.title,
            "summary": run.pending_human_review.summary,
        }
    else:
        result["pendingHumanReview"] = None
    if run.human_decision is not None:
        result["humanDecision"] = {
            "stepId": str(run.human_decision.step_id),
            "approved": run.human_decision.approved,
            "outcome": run.human_decision.outcome,
            "summary": run.human_decision.summary,
        }
    else:
        result["humanDecision"] = None
    return result


def _approval_json(approval: ApprovalRecord) -> dict[str, object]:
    """One complete request, as the client is shown it.

    Whole rather than incremental, like the content snapshots beside it: a
    client that reconnected mid-pause has no way to reconstruct a request from
    the parts of it that were emitted before it arrived.
    """
    return {
        "id": str(approval.approval_id),
        "status": approval.status.value,
        "kind": approval.kind.value,
        "reason": approval.reason,
        "command": approval.command,
        "cwd": approval.cwd,
        "toolName": approval.tool_name,
        "arguments": approval.arguments,
        "allowedDecisions": [
            decision.value for decision in approval.allowed_decisions
        ],
        "decision": approval.decision.value if approval.decision else None,
    }


def _messages_json(messages: tuple[Message, ...]) -> list[dict[str, object]]:
    """Group the engine's turn transcript into assistant-ui messages."""
    result: list[dict[str, object]] = []
    assistant_content: list[dict[str, object]] = []
    assistant_id = ""

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
        _merge_message(assistant_content, message)
    flush_assistant()
    return result


def _merge_message(content: list[dict[str, object]], message: Message) -> bool:
    """Fold one engine message into one assistant-ui assistant response."""
    changed = False
    if message.role is Role.ASSISTANT:
        if message.content:
            content.append({"type": "text", "text": message.content})
            changed = True
        for call in message.tool_calls:
            try:
                arguments = json.loads(call.arguments)
            except json.JSONDecodeError:
                arguments = {}
            if not isinstance(arguments, dict):
                arguments = {"value": arguments}
            content.append(
                {
                    "type": "tool-call",
                    "toolCallId": call.call_id,
                    "toolName": call.name,
                    "args": arguments,
                    "argsText": call.arguments,
                }
            )
            changed = True
    elif message.role is Role.TOOL and message.tool_call_id:
        for part in reversed(content):
            if part.get("toolCallId") == message.tool_call_id:
                part["result"] = message.content
                changed = True
                break
    return changed


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


def _thread_id(request: Request) -> AgentInstanceId:
    return AgentInstanceId(request.path_params["thread_id"])


def _new_agent_run_id() -> AgentRunId:
    return AgentRunId(f"ar-{uuid4().hex[:12]}")


def _json_line(value: dict[str, object]) -> bytes:
    return (json.dumps(value, separators=(",", ":")) + "\n").encode()


def _error(message: str, status_code: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status_code)


async def _missing_frontend(_request: Request) -> Response:
    return Response(
        "The assistant-ui client has not been built. Run `npm --prefix apps/web run build`.",
        status_code=503,
        media_type="text/plain",
    )


__all__ = ["ChatThread", "ThreadService", "create_app"]
