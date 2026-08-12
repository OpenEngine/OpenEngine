"""HTTP surface for the assistant-ui client.

The engine owns conversations; assistant-ui owns their presentation.  This
module translates between those two vocabularies and keeps the small amount of
thread metadata that is UI-specific (title, archive status, selected runner).

Runs are streamed as newline-delimited JSON.  Their tasks are owned by the
service rather than by one response, so a refreshed browser can reconnect.
A lock per thread prevents two turns from reading the same stale transcript.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator, Awaitable, Mapping
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from engine.domain import AgentId, AgentInstanceId, AgentRunId, Message, Role, WorkspaceId
from engine.ports import AgentRunner, WorkspaceState
from engine.runtime import AgentSession
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
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


class ActiveRun:
    """One agent turn whose lifetime is independent of an HTTP connection.

    Subscribers receive complete content snapshots, so a browser that refreshes
    can reconnect without needing to know which individual events it missed.
    """

    def __init__(self) -> None:
        self.content: list[dict[str, object]] = []
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
        while True:
            async with self._changed:
                await self._changed.wait_for(
                    lambda: self._revision > revision or self.done
                )
                revision = self._revision
                content = [dict(part) for part in self.content]
                error = self.error
                done = self.done

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
        return self._threads.get(instance_id)

    async def create(self, agent_id: AgentId, runner: str) -> ChatThread:
        await self._restore()
        if runner not in self.session.runners:
            raise ValueError(f"unknown runner {runner!r}")
        instance = await self.session.start(agent_id)
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
    ) -> str:
        thread = await self._require(instance_id)
        selected_runner = runner or thread.runner
        if selected_runner not in self.session.runners:
            raise ValueError(f"unknown runner {selected_runner!r}")
        thread.runner = selected_runner

        async with self._locks[instance_id]:
            turn = await self.session.say(
                instance_id,
                text,
                runner=selected_runner,
                on_message=observed.put_nowait,
            )
        return turn.message.content

    async def start_run(
        self, instance_id: AgentInstanceId, text: str, runner: str | None
    ) -> ActiveRun:
        await self._require(instance_id)
        await self._require_somewhere_to_run(instance_id)
        initial_message_count = len(await self.session.history(instance_id))
        current = self.active_run(instance_id)
        if current is not None:
            raise RuntimeError("this chat already has a run in progress")

        observed: asyncio.Queue[Message] = asyncio.Queue()
        run = ActiveRun()
        self._active_runs[instance_id] = run

        async def execute() -> str:
            task = asyncio.create_task(self.say(instance_id, text, runner, observed))
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
                AgentRunId(f"ar-{uuid4().hex[:12]}"),
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
        return thread.title

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
                    self.session.default_runner,
                )
                self._threads[instance.instance_id] = await self._sync_workspace(thread)
                self._locks[instance.instance_id] = asyncio.Lock()
            self._restored = True


def create_app(
    session: AgentSession,
    runners: Mapping[str, AgentRunner],
    static_directory: Path | None = None,
) -> Starlette:
    """Build the web application around already-composed capabilities."""
    service = ThreadService(session, runners)

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
            }
        )

    async def list_threads(_request: Request) -> JSONResponse:
        return JSONResponse({"threads": [_thread_json(t) for t in await service.list()]})

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
        thread = await service.get(_thread_id(request))
        if thread is None:
            return _error("thread not found", 404)
        body = await _json_body(request)
        if "title" in body:
            title = str(body["title"]).strip()
            if title:
                thread.title = title[:80]
        if "runner" in body:
            runner = str(body["runner"])
            if runner not in session.runners:
                return _error(f"unknown runner {runner!r}", 400)
            thread.runner = runner
        return JSONResponse(_thread_json(thread))

    async def archive_thread(request: Request) -> JSONResponse:
        thread = await service.get(_thread_id(request))
        if thread is None:
            return _error("thread not found", 404)
        thread.archived = request.scope["route"].name == "archive"
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
        run = service.active_run(instance_id)
        if run is not None:
            await run.cancel()
        return Response(status_code=204)

    routes = [
        Route("/api/config", config),
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
    ]
    if static_directory is not None and (static_directory / "index.html").is_file():
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
    return result


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
