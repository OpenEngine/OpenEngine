"""Run-bound workflow tools over MCP.

The provider CLI launches this module as a stdio MCP server. It deliberately
contains no workflow identifiers: an opaque credential connects it to the
in-process broker that already owns the run, agent run, and step context.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import sys
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

from engine.domain import AgentRunId, RunFailed, RunId, StepCompleted, StepSpec
from engine.domain.ids import WorkspaceId
from engine.ports import McpServerConfig, SourceControl
from engine.runtime.step_results import (
    InvalidStepResultError,
    run_failed_from_arguments,
    step_completed_from_arguments,
)

TerminalEvent = StepCompleted | RunFailed
TerminalDelivery = Callable[[TerminalEvent], Awaitable[None]]
McpRequestId = str | int

_SERVER_NAME = "workflow"
_PROTOCOL_VERSION = "2025-06-18"

#: Which `SourceControl` method each repository tool is a front for. A grant
#: is only served when the composed source control actually has its method, so
#: this is also the list of what "can this be served" is asked about -- one
#: table rather than a condition written out per tool.
REPOSITORY_TOOL_METHODS: dict[str, str] = {
    "git_subcommand": "run_git",
    "open_pull_request": "request_review",
    "add_comment": "add_comment",
}

#: The repository tools, in the order a server lists them.
REPOSITORY_TOOL_NAMES: tuple[str, ...] = tuple(REPOSITORY_TOOL_METHODS)

#: What `open_pull_request` proposes against when the agent names no base.
DEFAULT_BASE_REF = "main"


class TerminalResultAlreadySubmittedError(RuntimeError):
    """An agent run already owns an accepted terminal result."""


@dataclass(slots=True)
class TerminalResultRegistry:
    """Process-local single-submission guard shared by terminal sessions."""

    _accepted: dict[AgentRunId, TerminalEvent] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def accept(
        self,
        agent_run_id: AgentRunId,
        event: TerminalEvent,
        deliver: TerminalDelivery | None,
    ) -> None:
        async with self._lock:
            previous = self._accepted.get(agent_run_id)
            if previous is not None:
                raise TerminalResultAlreadySubmittedError(
                    "a terminal result was already accepted for this agent run"
                )
            if deliver is not None:
                await deliver(event)
            self._accepted[agent_run_id] = event


class TerminalMcpBroker:
    """Bind one local MCP bridge to one workflow execution context."""

    def __init__(
        self,
        *,
        run_id: RunId,
        agent_run_id: AgentRunId,
        step: StepSpec,
        registry: TerminalResultRegistry,
        deliver: TerminalDelivery | None = None,
    ) -> None:
        self._run_id = run_id
        self._agent_run_id = agent_run_id
        self._step = step
        self._registry = registry
        self._deliver = deliver
        # Hex rather than URL-safe base64, because this credential is handed to
        # the provider as an argv element: `token_urlsafe` can begin with `-`,
        # and roughly one session in sixty-four then had its server exit on
        # `--token: expected one argument` before answering `initialize`. Same
        # 256 bits, out of an alphabet nothing reads as an option.
        self._token = secrets.token_hex(32)
        self._server: asyncio.Server | None = None
        self._result: asyncio.Future[TerminalEvent] | None = None
        self._source_control: SourceControl | None = None
        self._repository_tools: tuple[str, ...] = ()
        self._workspace_id: WorkspaceId | None = None
        self._comments_added = 0

    def enable_repository_tools(
        self,
        source_control: SourceControl,
        names: Sequence[str],
        workspace_id: WorkspaceId | None = None,
    ) -> None:
        """Expose named repository operations through this run-bound server.

        `names` is what the step's profile was granted and the composition can
        honour, decided by the dispatcher; the broker only serves it. The
        workspace is the one the step is running in, and it is bound here
        rather than passed per call so a model cannot name a different one.
        """

        self._source_control = source_control
        self._repository_tools = tuple(
            name for name in REPOSITORY_TOOL_NAMES if name in names
        )
        self._workspace_id = workspace_id

    async def __aenter__(self) -> TerminalMcpBroker:
        self._result = asyncio.get_running_loop().create_future()
        self._server = await asyncio.start_server(
            self._handle_connection, "127.0.0.1", 0
        )
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        if self._result is not None and not self._result.done():
            self._result.cancel()

    @property
    def config(self) -> McpServerConfig:
        if self._server is None or not self._server.sockets:
            raise RuntimeError("terminal MCP broker has not been started")
        port = self._server.sockets[0].getsockname()[1]
        arguments = (
            "-m",
            "engine.runtime.terminal_mcp_server",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--token",
            self._token,
        )
        for name in self._repository_tools:
            arguments = (*arguments, "--repository-tool", name)
        return McpServerConfig(
            name=_SERVER_NAME,
            command=sys.executable,
            args=arguments,
        )

    async def result(self) -> TerminalEvent:
        if self._result is None:
            raise RuntimeError("terminal MCP broker has not been started")
        return await asyncio.shield(self._result)

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            raw = await reader.readline()
            request = json.loads(raw)
            response = await self._submit(request)
        except Exception as error:
            response = {"ok": False, "error": f"invalid terminal request: {error}"}
        writer.write(json.dumps(response, separators=(",", ":")).encode() + b"\n")
        with suppress(ConnectionError):
            await writer.drain()
        writer.close()
        with suppress(ConnectionError):
            await writer.wait_closed()

    async def _submit(self, request: object) -> dict[str, object]:
        if not isinstance(request, dict) or request.get("token") != self._token:
            return {"ok": False, "error": "terminal session is not authorized"}
        request_id = request.get("request_id")
        if (
            not isinstance(request_id, (str, int))
            or isinstance(request_id, bool)
        ):
            return {"ok": False, "error": "MCP request id must be a string or number"}
        name = request.get("name")
        arguments = request.get("arguments")
        try:
            if isinstance(name, str) and name in REPOSITORY_TOOL_NAMES:
                if name not in self._repository_tools:
                    return {"ok": False, "error": f"{name} is not enabled for this step"}
                return await self._repository_call(name, arguments)
            if name == "clarify":
                if not isinstance(arguments, dict) or arguments:
                    return {
                        "ok": False,
                        "error": "clarify does not accept arguments",
                    }
                return {"ok": True, "acknowledgement": "clarified"}
            if name == "complete_step":
                if "add_comment" in self._repository_tools and not self._comments_added:
                    return {
                        "ok": False,
                        "error": "add at least one pull-request comment before completing review",
                    }
                event: TerminalEvent = step_completed_from_arguments(
                    run_id=self._run_id,
                    step=self._step,
                    agent_run_id=self._agent_run_id,
                    arguments=arguments,
                    mcp_request_id=request_id,
                )
            elif name == "fail_step":
                event = run_failed_from_arguments(
                    run_id=self._run_id,
                    agent_run_id=self._agent_run_id,
                    arguments=arguments,
                    mcp_request_id=request_id,
                )
            else:
                return {"ok": False, "error": f"unknown terminal tool: {name}"}
            await self._registry.accept(
                self._agent_run_id, event, self._deliver
            )
        except (
            InvalidStepResultError,
            TerminalResultAlreadySubmittedError,
            ValueError,
        ) as error:
            return {"ok": False, "error": str(error)}
        assert self._result is not None
        if not self._result.done():
            self._result.set_result(event)
        return {"ok": True, "acknowledgement": "accepted"}

    async def _repository_call(
        self, name: str, arguments: object
    ) -> dict[str, object]:
        """Run one repository tool against the composed source control.

        Nothing here reaches a terminal result, so a failure is answered rather
        than raised: a rejected push or a `gh` that is not logged in is
        something the step can read and act on, not a reason to end it.
        """

        assert self._source_control is not None
        if name == "add_comment":
            pr_url, comment, file, line = _comment_arguments(arguments)
            try:
                await self._source_control.add_comment(pr_url, comment, file, line)
            except Exception as error:
                return {"ok": False, "error": f"could not add comment: {error}"}
            self._comments_added += 1
            return {"ok": True, "acknowledgement": "comment added"}

        if self._workspace_id is None:
            return {"ok": False, "error": f"{name} needs a workspace and this step has none"}

        if name == "git_subcommand":
            git_arguments = _git_arguments(arguments)
            try:
                result = await self._source_control.run_git(
                    self._workspace_id, git_arguments
                )
            except Exception as error:
                return {"ok": False, "error": f"could not run git: {error}"}
            reported = "\n".join(part for part in (result.stdout, result.stderr) if part)
            if not result.ok:
                return {
                    "ok": False,
                    "error": (
                        f"git exited {result.exit_code}: "
                        f"{reported or 'no output'}"
                    ),
                }
            # An empty answer is the normal one for half of git, and a tool
            # result with no text in it reads to a model as a tool that did
            # nothing. Say which command it was instead.
            return {
                "ok": True,
                "acknowledgement": "git ran",
                "output": reported or f"git {git_arguments[0]} exited 0 with no output",
            }

        branch, base_ref, title, body = _review_arguments(arguments)
        try:
            url = await self._source_control.request_review(
                self._workspace_id, branch, base_ref, title, body
            )
        except Exception as error:
            return {"ok": False, "error": f"could not open the pull request: {error}"}
        return {"ok": True, "acknowledgement": "pull request opened", "output": url}


def terminal_tool_names(repository_tools: Sequence[str] = ()) -> tuple[str, ...]:
    """The tools a step's server serves, in the order it lists them.

    Read off the listing rather than restated beside it, so a tool added to
    `_tools` cannot end up served without the step being told it holds one.
    """
    return tuple(str(tool["name"]) for tool in _tools(repository_tools))


def _tools(repository_tools: Sequence[str] = ()) -> list[dict[str, object]]:
    tools: list[dict[str, object]] = [
        {
            "name": "complete_step",
            "description": "Complete the bound workflow step.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "outcome": {"type": "string", "enum": ["success"]},
                    "summary": {"type": "string"},
                    "outputs": {
                        "type": "object",
                        "additionalProperties": {"type": "string"},
                    },
                },
                "required": ["outcome", "summary", "outputs"],
                "additionalProperties": False,
            },
        },
        {
            "name": "fail_step",
            "description": "Fail the bound workflow run when the step cannot continue.",
            "inputSchema": {
                "type": "object",
                "properties": {"summary": {"type": "string", "minLength": 1}},
                "required": ["summary"],
                "additionalProperties": False,
            },
        },
        {
            "name": "clarify",
            "description": (
                "Finish answering a human question without changing workflow "
                "run state. Call this after the answer when no implementation "
                "change was requested or made."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
    ]
    tools.extend(
        _REPOSITORY_TOOLS[name]
        for name in REPOSITORY_TOOL_NAMES
        if name in repository_tools
    )
    return tools


#: The repository tools' declarations, by name.
#:
#: `git_subcommand` is a passthrough rather than one entry per operation, and
#: that is the point of it: git is the interface an agent already knows, and a
#: menu of `create_branch`/`commit`/`push` would keep meeting work that needs
#: the twentieth subcommand nobody put on the menu -- a rebase, a cherry-pick,
#: a `log -S` to find where something went. What is bounded is the checkout it
#: runs in, which the broker holds and the model cannot name.
_REPOSITORY_TOOLS: dict[str, dict[str, object]] = {
    "git_subcommand": {
        "name": "git_subcommand",
        "description": (
            "Run git in this step's workspace. `arguments` is everything that "
            "would follow `git`, one element per argument: "
            '["commit", "-m", "feat: add the thing"]. Any subcommand is '
            "available. No shell is involved, so quoting, globbing, pipes and "
            "redirection do not apply -- a multi-line commit message is simply "
            "one element. Returns git's output; a non-zero exit is reported as "
            "an error with whatever git printed. Two refusals: -C, --git-dir "
            "and --work-tree, because the workspace is not yours to change; "
            "and pushing Engine's internal engine/ branch, so branch to a "
            "descriptive name such as agent/<description> before publishing."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "arguments": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                }
            },
            "required": ["arguments"],
            "additionalProperties": False,
        },
    },
    "open_pull_request": {
        "name": "open_pull_request",
        "description": (
            "Open a pull request for a branch already pushed to the remote, "
            "and return its URL. Push the branch with git_subcommand first."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "branch": {"type": "string", "minLength": 1},
                "base_ref": {"type": "string", "minLength": 1},
                "title": {"type": "string", "minLength": 1},
                "body": {"type": "string"},
            },
            "required": ["branch", "title", "body"],
            "additionalProperties": False,
        },
    },
    "add_comment": {
        "name": "add_comment",
        "description": (
            "Add a comment to a pull request. Provide file and line together "
            "for an inline comment; omit both for a general comment."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "pr_url": {"type": "string", "minLength": 1},
                "comment": {"type": "string", "minLength": 1},
                "file": {"type": "string", "minLength": 1},
                "line": {"type": "integer", "minimum": 1},
            },
            "required": ["pr_url", "comment"],
            "dependentRequired": {"file": ["line"], "line": ["file"]},
            "additionalProperties": False,
        },
    },
}


def _git_arguments(arguments: object) -> tuple[str, ...]:
    if not isinstance(arguments, dict):
        raise ValueError("git_subcommand arguments must be an object")
    unexpected = set(arguments) - {"arguments"}
    if unexpected:
        names = ", ".join(sorted(str(name) for name in unexpected))
        raise ValueError(f"unexpected git_subcommand arguments: {names}")
    given = arguments.get("arguments")
    if not isinstance(given, list) or not given:
        raise ValueError("arguments must be a non-empty array of strings")
    if not all(isinstance(argument, str) for argument in given):
        raise ValueError("every element of arguments must be a string")
    return tuple(given)


def _review_arguments(arguments: object) -> tuple[str, str, str, str]:
    if not isinstance(arguments, dict):
        raise ValueError("open_pull_request arguments must be an object")
    unexpected = set(arguments) - {"branch", "base_ref", "title", "body"}
    if unexpected:
        names = ", ".join(sorted(str(name) for name in unexpected))
        raise ValueError(f"unexpected open_pull_request arguments: {names}")
    branch = arguments.get("branch")
    base_ref = arguments.get("base_ref", DEFAULT_BASE_REF)
    title = arguments.get("title")
    body = arguments.get("body", "")
    if not isinstance(branch, str) or not branch.strip():
        raise ValueError("branch must be a non-empty string")
    if not isinstance(base_ref, str) or not base_ref.strip():
        raise ValueError("base_ref must be a non-empty string")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("title must be a non-empty string")
    if not isinstance(body, str):
        raise ValueError("body must be a string")
    return branch, base_ref, title, body


def _comment_arguments(
    arguments: object,
) -> tuple[str, str, str | None, int | None]:
    if not isinstance(arguments, dict):
        raise ValueError("add_comment arguments must be an object")
    unexpected = set(arguments) - {"pr_url", "comment", "file", "line"}
    if unexpected:
        names = ", ".join(sorted(str(name) for name in unexpected))
        raise ValueError(f"unexpected add_comment arguments: {names}")
    pr_url = arguments.get("pr_url")
    comment = arguments.get("comment")
    file = arguments.get("file")
    line = arguments.get("line")
    if not isinstance(pr_url, str) or not pr_url.strip():
        raise ValueError("pr_url must be a non-empty string")
    if not isinstance(comment, str) or not comment.strip():
        raise ValueError("comment must be a non-empty string")
    if file is not None and (not isinstance(file, str) or not file.strip()):
        raise ValueError("file must be a non-empty string")
    if line is not None and (
        not isinstance(line, int) or isinstance(line, bool) or line < 1
    ):
        raise ValueError("line must be a positive integer")
    if (file is None) != (line is None):
        raise ValueError("file and line must be provided together")
    return pr_url, comment, file, line


async def _forward_call(
    host: str,
    port: int,
    token: str,
    request_id: McpRequestId,
    name: object,
    arguments: object,
) -> dict[str, object]:
    reader, writer = await asyncio.open_connection(host, port)
    writer.write(
        json.dumps(
            {
                "token": token,
                "request_id": request_id,
                "name": name,
                "arguments": arguments,
            },
            separators=(",", ":"),
        ).encode()
        + b"\n"
    )
    await writer.drain()
    response = json.loads(await reader.readline())
    writer.close()
    with suppress(ConnectionError):
        await writer.wait_closed()
    return response


async def _serve_stdio(
    host: str, port: int, token: str, *, repository_tools: Sequence[str] = ()
) -> None:
    """Serve newline-delimited MCP JSON-RPC without writing logs to stdout."""
    while line := await asyncio.to_thread(sys.stdin.buffer.readline):
        try:
            request: Any = json.loads(line)
            response = await _mcp_response(
                host, port, token, request, repository_tools=repository_tools
            )
            if response is None:
                continue
        except Exception as error:
            response = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": f"Parse error: {error}"},
            }
        sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
        sys.stdout.flush()


async def _mcp_response(
    host: str,
    port: int,
    token: str,
    request: object,
    *,
    repository_tools: Sequence[str] = (),
) -> dict[str, object] | None:
    if not isinstance(request, dict):
        return _rpc_error(None, -32600, "Invalid Request")
    request_id = request.get("id")
    method = request.get("method")
    if isinstance(method, str) and method.startswith("notifications/"):
        # JSON-RPC notifications never receive responses.
        return None
    if method == "initialize":
        requested = request.get("params")
        protocol = (
            requested.get("protocolVersion", _PROTOCOL_VERSION)
            if isinstance(requested, dict)
            else _PROTOCOL_VERSION
        )
        return _rpc_result(
            request_id,
            {
                "protocolVersion": protocol,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "engine-workflow-terminal", "version": "1"},
            },
        )
    if method == "ping":
        return _rpc_result(request_id, {})
    if method == "tools/list":
        return _rpc_result(request_id, {"tools": _tools(repository_tools)})
    if method != "tools/call":
        return _rpc_error(request_id, -32601, "Method not found")
    if not isinstance(request_id, (str, int)) or isinstance(request_id, bool):
        return _rpc_error(request_id, -32600, "Tool calls require a request id")
    params = request.get("params")
    if not isinstance(params, dict):
        return _rpc_error(request_id, -32602, "Invalid tool parameters")
    forwarded = await _forward_call(
        host,
        port,
        token,
        request_id,
        params.get("name"),
        params.get("arguments", {}),
    )
    if forwarded.get("ok") is True:
        # Terminal tools acknowledge and nothing more; a repository tool has an
        # answer the step needs -- git's output, a pull-request URL -- and
        # returning a bare "accepted" for those would make the model guess at
        # what its own command printed.
        output = forwarded.get("output")
        if isinstance(output, str) and output:
            return _rpc_result(
                request_id,
                {
                    "content": [{"type": "text", "text": output}],
                    "structuredContent": {"accepted": True, "output": output},
                },
            )
        return _rpc_result(
            request_id,
            {
                "content": [{"type": "text", "text": "accepted"}],
                "structuredContent": {"accepted": True},
            },
        )
    return _rpc_result(
        request_id,
        {
            "content": [{"type": "text", "text": str(forwarded.get("error"))}],
            "isError": True,
        },
    )


def _rpc_result(request_id: object, result: object) -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _rpc_error(request_id: object, code: int, message: str) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--token", required=True)
    parser.add_argument(
        "--repository-tool",
        action="append",
        default=[],
        choices=REPOSITORY_TOOL_NAMES,
        dest="repository_tools",
    )
    args = parser.parse_args()
    asyncio.run(
        _serve_stdio(
            args.host,
            args.port,
            args.token,
            repository_tools=tuple(args.repository_tools),
        )
    )


__all__ = [
    "DEFAULT_BASE_REF",
    "REPOSITORY_TOOL_METHODS",
    "REPOSITORY_TOOL_NAMES",
    "TerminalEvent",
    "TerminalMcpBroker",
    "TerminalResultAlreadySubmittedError",
    "TerminalResultRegistry",
    "terminal_tool_names",
]
