"""An ACP agent running as a child process, spoken to over its stdio.

This is the concrete half of `ACPClient` and `ACPSession`: launch a command,
speak JSON-RPC on its pipes, and shut it down without leaving it running. Every
ACP CLI in circulation is reached this way, so one implementation serves Codex,
Claude, and whatever registers next -- the per-agent part is a command line, not
a class.

Two decisions here are worth stating rather than discovering:

Incoming requests are answered, not ignored. The client advertises no
filesystem and no terminal, so a conforming agent will not ask for either, and
anything it does ask for that this version has no answer for gets a JSON-RPC
"method not found" instead of silence -- an agent waiting forever on a request
nobody will answer is the worst way for a gap to show up. Permission requests
are the exception that proves it: they are streamed as `acp.permission.requested`
and then handed to whatever policy the connection was built with, which is
`deny_permission` unless the caller supplied one. See
`langgraph_acp.permissions`.

The child's stderr is drained into a small ring buffer. When a launch fails, a
process dies mid-request, or an agent answers "internal error" without saying
what was internal, that tail is often the only thing that says why -- and a pipe
nobody reads eventually blocks the process writing into it. Where that text is
allowed to go is a decision rather than a detail; see `_refusal`.
"""

import asyncio
import json
import logging
import os
from collections import deque
from collections.abc import AsyncGenerator, Mapping, Sequence
from dataclasses import dataclass, field

from langgraph_acp._json import (
    JSONObject,
    JSONValue,
    as_mapping,
    checked_sequence,
    copied_mapping,
)
from langgraph_acp._jsonrpc import METHOD_NOT_FOUND, JSONRPCError, JSONRPCPeer
from langgraph_acp.client import PROTOCOL_VERSION, ACPCapabilities, ACPClient
from langgraph_acp.errors import (
    ACPAgentCapabilityError,
    ACPConnectionError,
    ACPError,
    ACPSessionError,
)
from langgraph_acp.events import ACPEvent, ACPEventType
from langgraph_acp.permissions import (
    ACPPermissionHandler,
    ACPPermissionRequest,
    deny_permission,
)
from langgraph_acp.session import ACPPrompt, ACPSession

#: Room for one JSON-RPC message. ACP carries file contents and diffs inline,
#: so the 64 KiB an asyncio stream defaults to is not enough by a wide margin.
MAX_MESSAGE_BYTES = 8 * 1024 * 1024

#: How much of a failed agent's complaint to keep for the error message.
STDERR_TAIL_LINES = 20

#: How many characters of one of those parts to keep. An agent's error `data`
#: can be an entire HTTP response body and a single stderr line can be the same,
#: while this text ends up in a run record and in a log line; enough to name the
#: cause is the useful amount.
MAX_DETAIL_CHARS = 2000

#: Where this module says what it could not put in an exception.
log = logging.getLogger(__name__)

#: What this client tells an agent it can do. Nothing, for now: the filesystem
#: and terminal methods an agent may call belong to tickets that have not
#: happened, and advertising a capability this client cannot honour would turn a
#: clean "not supported" into a hung request.
CLIENT_CAPABILITIES: JSONObject = {
    "fs": {"readTextFile": False, "writeTextFile": False},
    "terminal": False,
}

#: ACP session updates, mapped onto this package's event vocabulary. Anything
#: absent here reaches the consumer as `acp.raw` rather than being dropped;
#: filling the table in -- tool completion, message completion -- is the
#: streaming ticket's work.
_UPDATE_EVENTS: Mapping[str, ACPEventType] = {
    "agent_message_chunk": ACPEventType.MESSAGE_DELTA,
    "agent_thought_chunk": ACPEventType.THOUGHT_DELTA,
    "tool_call": ACPEventType.TOOL_STARTED,
    "tool_call_update": ACPEventType.TOOL_UPDATED,
    "plan": ACPEventType.PLAN_UPDATED,
    "usage_update": ACPEventType.USAGE_UPDATED,
    "current_mode_update": ACPEventType.CONFIG_UPDATED,
}


@dataclass(frozen=True, slots=True)
class _Completion:
    """The end of a turn, queued behind the events that preceded it."""

    result: JSONObject = field(default_factory=dict)
    error: Exception | None = None


async def connect_over_stdio(
    *,
    agent: str,
    command: Sequence[str],
    env: Mapping[str, str] | None = None,
    cwd: str | os.PathLike[str] | None = None,
    permissions: ACPPermissionHandler | None = None,
) -> ACPClient:
    """Launch `command` and initialize ACP against it.

    `env` is overlaid on this process's environment rather than replacing it: a
    command found through `PATH` stops being findable the moment `PATH` is not
    inherited, and every agent CLI is found that way.

    `permissions` answers `session/request_permission`; without one the
    connection declines every request. See `langgraph_acp.permissions`.
    """
    launched = tuple(checked_sequence(command, field="command"))
    try:
        process = await asyncio.create_subprocess_exec(
            *launched,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=None if env is None else {**os.environ, **env},
            cwd=None if cwd is None else os.fspath(cwd),
            limit=MAX_MESSAGE_BYTES,
        )
    except OSError as exc:
        raise ACPConnectionError(
            f"could not start the ACP agent: {' '.join(launched)!r} ({exc})",
            agent=agent,
            operation="connect",
        ) from exc

    try:
        client = StdioACPClient(
            agent=agent, process=process, permissions=permissions
        )
    except BaseException:
        process.kill()
        raise
    try:
        await client.initialize()
    except BaseException:
        await client.close()
        raise
    return client


class StdioACPClient:
    """An `ACPClient` backed by a child process speaking ACP on its pipes."""

    def __init__(
        self,
        *,
        agent: str,
        process: asyncio.subprocess.Process,
        permissions: ACPPermissionHandler | None = None,
    ) -> None:
        if process.stdin is None or process.stdout is None:
            raise ACPConnectionError(
                "the ACP agent was launched without the pipes the protocol needs",
                agent=agent,
                operation="connect",
            )
        self._agent = agent
        self._permissions = permissions or deny_permission
        self._process = process
        self._capabilities = ACPCapabilities()
        self._stderr: deque[str] = deque(maxlen=STDERR_TAIL_LINES)
        self._streams: dict[str, asyncio.Queue[ACPEvent | _Completion]] = {}
        self._closed = False
        self._draining = asyncio.create_task(self._drain_stderr())
        self._peer = JSONRPCPeer(
            process.stdout,
            process.stdin,
            on_request=self._on_request,
            on_notification=self._on_notification,
            on_closed=self._closed_error,
            on_eof=self._settle,
        )
        self._peer.start()

    @property
    def agent(self) -> str:
        return self._agent

    @property
    def capabilities(self) -> ACPCapabilities:
        return self._capabilities

    async def initialize(self) -> ACPCapabilities:
        """Perform the ACP handshake and record what the agent advertised."""
        response = await self.call(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "clientCapabilities": CLIENT_CAPABILITIES,
            },
        )
        self._capabilities = ACPCapabilities.from_initialize_response(
            as_mapping(response, field="the initialize result")
        )
        return self._capabilities

    async def new_session(
        self,
        *,
        cwd: str | os.PathLike[str] | None = None,
        mcp_servers: Sequence[Mapping[str, JSONValue]] = (),
    ) -> ACPSession:
        response = as_mapping(
            await self.call(
                "session/new",
                {"cwd": _working_directory(cwd), "mcpServers": _servers(mcp_servers)},
                failure=ACPSessionError,
            ),
            field="the session/new result",
        )
        session_id = response.get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            raise ACPSessionError(
                "the agent started a session without naming it",
                agent=self._agent,
                operation="session/new",
            )
        return StdioACPSession(self, session_id)

    async def resume_session(
        self,
        session_id: str,
        *,
        cwd: str | os.PathLike[str] | None = None,
        mcp_servers: Sequence[Mapping[str, JSONValue]] = (),
    ) -> ACPSession:
        if not self._capabilities.load_session:
            raise ACPAgentCapabilityError(
                f"agent {self._agent!r} cannot resume a session: it does not "
                "advertise the loadSession capability",
                agent=self._agent,
                session_id=session_id,
                operation="session/load",
            )
        await self.call(
            "session/load",
            {
                "sessionId": session_id,
                "cwd": _working_directory(cwd),
                "mcpServers": _servers(mcp_servers),
            },
            session_id=session_id,
            failure=ACPSessionError,
        )
        return StdioACPSession(self, session_id)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._streams.clear()
        await self._peer.aclose()
        await self._reap()
        self._draining.cancel()

    async def call(
        self,
        method: str,
        params: JSONObject,
        *,
        session_id: str | None = None,
        failure: type[ACPError] = ACPConnectionError,
    ) -> JSONValue:
        """One ACP request, with its failures named after the thing that failed."""
        if self._closed:
            raise ACPConnectionError(
                f"the connection to agent {self._agent!r} is closed",
                agent=self._agent,
                session_id=session_id,
                operation=method,
            )
        try:
            return await self._peer.request(method, params)
        except JSONRPCError as exc:
            raise failure(
                self._refusal(method, exc, session_id),
                agent=self._agent,
                session_id=session_id,
                operation=method,
            ) from exc
        except ACPError as exc:
            # Raised by `_closed_error` when the process stopped answering. It
            # already knows why; only the operation it interrupted is missing.
            if exc.operation is None:
                exc.operation = method
                exc.session_id = exc.session_id or session_id
            raise

    def notify_nowait(self, method: str, params: JSONObject) -> None:
        self._peer.notify_nowait(method, params)

    async def notify(self, method: str, params: JSONObject) -> None:
        await self._peer.notify(method, params)

    def open_stream(
        self, session_id: str
    ) -> asyncio.Queue[ACPEvent | _Completion]:
        """Claim this session's updates for the turn about to start."""
        if session_id in self._streams:
            raise ACPSessionError(
                "a prompt is already running in this session; ACP runs one turn "
                "at a time, so the running one has to end or be cancelled first",
                agent=self._agent,
                session_id=session_id,
                operation="session/prompt",
            )
        stream: asyncio.Queue[ACPEvent | _Completion] = asyncio.Queue()
        self._streams[session_id] = stream
        return stream

    def close_stream(self, session_id: str) -> None:
        self._streams.pop(session_id, None)

    def event(
        self, session_id: str | None, event_type: str, data: Mapping[str, JSONValue]
    ) -> ACPEvent:
        return ACPEvent(
            agent=self._agent, type=event_type, session_id=session_id, data=data
        )

    async def _on_request(self, method: str, params: JSONObject) -> JSONValue:
        if method == "session/request_permission":
            session_id = _session_id_of(params)
            self._deliver(
                session_id,
                self.event(session_id, ACPEventType.PERMISSION_REQUESTED, params),
            )
            # Streamed first, answered second, and in that order deliberately: a
            # handler that waits for a person may take days or never return at
            # all, and a subscriber that only learned about the request once it
            # had been settled could not show anyone the question.
            outcome = await self._permissions(
                ACPPermissionRequest.from_params(self._agent, params)
            )
            self._deliver(
                session_id,
                self.event(
                    session_id,
                    ACPEventType.PERMISSION_RESOLVED,
                    {
                        "sessionId": session_id,
                        "optionId": outcome.option_id,
                        "granted": outcome.granted,
                    },
                ),
            )
            return outcome.to_acp()
        raise JSONRPCError(
            METHOD_NOT_FOUND,
            f"langgraph-acp does not implement {method}",
        )

    def _on_notification(self, method: str, params: JSONObject) -> None:
        # Total by construction. This runs on the read loop, where an exception
        # would take the connection down over a notification nobody had to
        # understand, so anything unrecognized falls through to `acp.raw`.
        session_id = _session_id_of(params)
        if method == "session/update" and isinstance(params.get("update"), Mapping):
            update = as_mapping(params.get("update"), field="update")
            kind = update.get("sessionUpdate")
            event_type = _UPDATE_EVENTS.get(
                kind if isinstance(kind, str) else "", ACPEventType.RAW
            )
            self._deliver(session_id, self.event(session_id, event_type, update))
            return
        self._deliver(
            session_id,
            self.event(
                session_id, ACPEventType.RAW, {"method": method, "params": params}
            ),
        )

    def _deliver(self, session_id: str | None, event: ACPEvent) -> None:
        # An update belongs to whichever turn is streaming that session. Updates
        # that belong to no turn -- the history an agent replays while loading a
        # session, notably -- have nowhere to go until the streaming ticket
        # gives them one, so they are dropped rather than misfiled.
        stream = self._streams.get(session_id) if session_id is not None else None
        if stream is not None:
            stream.put_nowait(event)

    async def _drain_stderr(self) -> None:
        stream = self._process.stderr
        if stream is None:
            return
        try:
            while True:
                line = await stream.readline()
                if not line:
                    return
                self._stderr.append(line.decode(errors="replace").rstrip())
        except (ValueError, ConnectionError):
            return

    async def _settle(self) -> None:
        """Let a dying process finish dying, so the error can say how it died.

        Stdout closing is the first sign the agent is gone, and it arrives
        before the exit status and before the last of stderr. Failing the
        pending requests immediately would produce the one error message that
        cannot explain itself.
        """
        try:
            await asyncio.wait_for(self._process.wait(), timeout=5)
        except TimeoutError:
            return
        await asyncio.wait({self._draining}, timeout=5)

    def _stderr_tail(self) -> str:
        """What the child has said lately. Empty when it has said nothing.

        Trimmed from the front, because the interesting end of a process's
        output is the end it stopped at.
        """
        return _last("\n".join(self._stderr))

    def _refusal(self, method: str, exc: JSONRPCError, session_id: str | None) -> str:
        """A refusal, rendered with the parts of it that say what failed.

        Only `message` is required to be legible, and agents routinely send one
        that is not: `-32603 "Internal error"` is what an adapter answers when
        something underneath *it* failed, and the sentence naming the cause is
        in `data`, or in what the process wrote to stderr on its way past. The
        Codex adapter rejecting a model it is too old to run puts the upstream
        400 in both and neither in `message`, so a message built from `message`
        alone rendered every such failure identically.

        `data` goes in the message and the stderr tail does not, and the split
        is deliberate. This text is caught by callers that store it and show it:
        a graph run keeps it as the reason it failed, and whoever opened the
        work reads it there. `data` is the agent's own account of the request it
        just refused. The tail is neither -- it is whatever the process happened
        to write lately, which is arbitrary output, and one connection carries
        several sessions, so it may belong to a conversation other than this
        one. That belongs in an operator's log, and the message says where it
        went rather than carrying it.
        """
        rendered = f"the agent refused {method}: {exc.message} (code {exc.code})"
        if exc.data is not None:
            rendered += f"; data: {_capped(json.dumps(exc.data))}"
        tail = self._stderr_tail()
        if not tail:
            return rendered
        log.error(
            "agent %r refused %s (session %s); the agent process's recent "
            "output was:\n%s",
            self._agent,
            method,
            session_id or "none",
            tail,
        )
        return rendered + "; the agent process's recent output is in the log"

    def _closed_error(self) -> BaseException:
        status = self._process.returncode
        ended = (
            f"exited with status {status}"
            if status is not None
            else "stopped answering"
        )
        # Here the tail *is* the diagnosis and there is no next request to carry
        # one: a process that died wrote its reason on the way out, and nothing
        # else in this failure says what it was.
        tail = self._stderr_tail()
        return ACPConnectionError(
            f"the ACP agent {ended}"
            + (f"; its last output was:\n{tail}" if tail else ""),
            agent=self._agent,
        )

    async def _reap(self) -> None:
        """Give the agent its chance to exit on EOF, then insist."""
        for stop in (None, self._process.terminate, self._process.kill):
            if self._process.returncode is not None:
                return
            try:
                if stop is not None:
                    stop()
                await asyncio.wait_for(self._process.wait(), timeout=5)
                return
            except (TimeoutError, ProcessLookupError):
                continue


class StdioACPSession:
    """An `ACPSession` on a `StdioACPClient`."""

    def __init__(self, client: StdioACPClient, session_id: str) -> None:
        self._client = client
        self._session_id = session_id

    @property
    def session_id(self) -> str:
        return self._session_id

    async def prompt(self, prompt: ACPPrompt) -> AsyncGenerator[ACPEvent, None]:
        """Run one turn, yielding its updates and then its completion.

        The turn runs as its own task so updates can be yielded while the
        request that provoked them is still open -- that request only returns
        once the agent has finished, which is far too late to be watching.
        """
        blocks = _content_blocks(prompt)
        stream = self._client.open_stream(self._session_id)
        turn = asyncio.create_task(self._run(blocks, stream))
        try:
            while True:
                item = await stream.get()
                if isinstance(item, ACPEvent):
                    yield item
                    continue
                if item.error is not None:
                    raise item.error
                yield self._client.event(
                    self._session_id, ACPEventType.PROMPT_COMPLETED, item.result
                )
                return
        finally:
            self._client.close_stream(self._session_id)
            if not turn.done():
                # The consumer stopped early or was cancelled. Tell the agent
                # before dropping the request, or it keeps working for nobody.
                # Nothing is awaited here: unwinding a cancellation is the one
                # path where every suspension point can be cancelled again.
                self._client.notify_nowait(
                    "session/cancel", {"sessionId": self._session_id}
                )
                turn.cancel()

    async def cancel(self) -> None:
        await self._client.notify("session/cancel", {"sessionId": self._session_id})

    async def close(self) -> None:
        self._client.close_stream(self._session_id)

    async def _run(
        self, blocks: Sequence[JSONObject], stream: asyncio.Queue[ACPEvent | _Completion]
    ) -> None:
        try:
            result = await self._client.call(
                "session/prompt",
                {"sessionId": self._session_id, "prompt": list(blocks)},
                session_id=self._session_id,
                failure=ACPSessionError,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            stream.put_nowait(_Completion(error=exc))
        else:
            stream.put_nowait(
                _Completion(result=as_mapping(result, field="the session/prompt result"))
            )


def _capped(detail: str) -> str:
    """Enough of a detail to name a cause, and never a whole response body.

    Kept from the start, which is where a structured error puts its type and
    its message and where a response body puts everything worth reading.
    """
    if len(detail) <= MAX_DETAIL_CHARS:
        return detail
    dropped = len(detail) - MAX_DETAIL_CHARS
    return f"{detail[:MAX_DETAIL_CHARS]}... ({dropped} more characters)"


def _last(detail: str) -> str:
    """The same, kept from the other end.

    What a process wrote last is what it wrote about the thing that just went
    wrong; trimming a dying agent's output to its first two thousand characters
    would drop exactly the part worth having.
    """
    if len(detail) <= MAX_DETAIL_CHARS:
        return detail
    dropped = len(detail) - MAX_DETAIL_CHARS
    return f"({dropped} earlier characters) ...{detail[-MAX_DETAIL_CHARS:]}"


def _content_blocks(prompt: ACPPrompt) -> tuple[JSONObject, ...]:
    """Text, or the content blocks a caller assembled, as ACP wants them."""
    if isinstance(prompt, str):
        return ({"type": "text", "text": prompt},)
    return tuple(
        copied_mapping(block) for block in checked_sequence(prompt, field="prompt")
    )


def _working_directory(cwd: str | os.PathLike[str] | None) -> str:
    """ACP wants an absolute path, and wants one even when the caller had none."""
    return os.path.abspath(os.fspath(cwd) if cwd is not None else os.getcwd())


def _servers(servers: Sequence[Mapping[str, JSONValue]]) -> list[JSONObject]:
    return [
        copied_mapping(server)
        for server in checked_sequence(servers, field="mcp_servers")
    ]


def _session_id_of(params: Mapping[str, JSONValue]) -> str | None:
    session_id = params.get("sessionId")
    return session_id if isinstance(session_id, str) else None


__all__ = ["StdioACPClient", "StdioACPSession", "connect_over_stdio"]
