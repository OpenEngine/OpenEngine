"""HTTP surface for the assistant-ui client.

The engine owns conversations; assistant-ui owns their presentation.  This
module translates between those two vocabularies and keeps the small amount of
thread metadata that is UI-specific (title, archive status, selected runner).

Runs are streamed as newline-delimited JSON.  Each request remains attached to
its own ``AgentSession.say`` task, so several threads can be running at once.
A lock per thread prevents two turns from reading the same stale transcript.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from pathlib import Path

from engine.domain import AgentId, AgentInstanceId, Message, Role
from engine.ports import AgentRunner
from engine.runtime import AgentSession
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles


@dataclass(slots=True)
class ChatThread:
    """UI metadata for one engine agent instance."""

    instance_id: AgentInstanceId
    agent_id: AgentId
    runner: str
    title: str = "New chat"
    archived: bool = False


class ThreadService:
    """Coordinates assistant-ui threads over an ``AgentSession``."""

    def __init__(self, session: AgentSession) -> None:
        self.session = session
        self._threads: dict[AgentInstanceId, ChatThread] = {}
        self._locks: dict[AgentInstanceId, asyncio.Lock] = {}
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
        self._threads[instance.instance_id] = thread
        self._locks[instance.instance_id] = asyncio.Lock()
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

    async def _require(self, instance_id: AgentInstanceId) -> ChatThread:
        thread = await self.get(instance_id)
        if thread is None:
            raise KeyError(f"no chat thread {instance_id!r}")
        return thread

    async def _restore(self) -> None:
        """Populate the UI registry from the durable conversation store once."""
        if self._restored:
            return
        async with self._restore_lock:
            if self._restored:
                return
            instances = await self.session.instances()
            for instance in reversed(instances):
                self._threads[instance.instance_id] = ChatThread(
                    instance.instance_id,
                    instance.agent_id,
                    self.session.default_runner,
                )
                self._locks[instance.instance_id] = asyncio.Lock()
            self._restored = True


def create_app(
    session: AgentSession,
    runners: Mapping[str, AgentRunner],
    static_directory: Path | None = None,
) -> Starlette:
    """Build the web application around already-composed capabilities."""
    service = ThreadService(session)

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
        try:
            history = await service.history(_thread_id(request))
        except KeyError:
            return _error("thread not found", 404)
        return JSONResponse({"messages": _messages_json(history)})

    async def title_thread(request: Request) -> JSONResponse:
        thread = await service.get(_thread_id(request))
        if thread is None:
            return _error("thread not found", 404)
        body = await _json_body(request)
        title = _title_from_messages(body.get("messages", ()))
        if title:
            thread.title = title
        return JSONResponse({"title": thread.title})

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

        async def stream() -> AsyncIterator[bytes]:
            observed: asyncio.Queue[Message] = asyncio.Queue()
            task = asyncio.create_task(service.say(instance_id, text, runner, observed))
            content: list[dict[str, object]] = []
            try:
                while not task.done() or not observed.empty():
                    try:
                        message = await asyncio.wait_for(observed.get(), timeout=0.1)
                    except asyncio.TimeoutError:
                        continue
                    if _merge_message(content, message):
                        yield _json_line({"type": "content", "content": content})

                answer = await task
                if answer and not any(
                    part.get("type") == "text" and part.get("text") == answer
                    for part in content
                ):
                    content.append({"type": "text", "text": answer})
                yield _json_line({"type": "done", "content": content})
            except asyncio.CancelledError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise
            except Exception as error:  # the stream reports model/CLI failures
                yield _json_line(
                    {"type": "error", "error": f"{type(error).__name__}: {error}"}
                )

        return StreamingResponse(stream(), media_type="application/x-ndjson")

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
        Route("/api/threads/{thread_id}/title", title_thread, methods=["POST"]),
        Route("/api/threads/{thread_id}/runs", run_thread, methods=["POST"]),
    ]
    if static_directory is not None and (static_directory / "index.html").is_file():
        routes.append(Mount("/", StaticFiles(directory=static_directory, html=True)))
    else:
        routes.append(Route("/", _missing_frontend))
    app = Starlette(routes=routes)
    app.state.thread_service = service
    return app


def _thread_json(thread: ChatThread) -> dict[str, object]:
    return {
        "id": str(thread.instance_id),
        "title": thread.title,
        "archived": thread.archived,
        "agentId": str(thread.agent_id),
        "runner": thread.runner,
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


def _title_from_messages(messages: object) -> str:
    if not isinstance(messages, list):
        return ""
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content", ())
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = " ".join(
                str(part.get("text", ""))
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            )
        else:
            text = ""
        compact = " ".join(text.split())
        if compact:
            return compact[:48] + ("…" if len(compact) > 48 else "")
    return ""


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
