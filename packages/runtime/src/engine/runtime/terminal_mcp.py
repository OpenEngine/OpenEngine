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
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

from engine.domain import AgentRunId, RunFailed, RunId, StepCompleted, StepSpec
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
        self._token = secrets.token_urlsafe(32)
        self._server: asyncio.Server | None = None
        self._result: asyncio.Future[TerminalEvent] | None = None
        self._source_control: SourceControl | None = None
        self._comments_added = 0

    def enable_repo_comments(self, source_control: SourceControl) -> None:
        """Expose repository commenting through this run-bound MCP server."""

        self._source_control = source_control

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
        if self._source_control is not None:
            arguments = (*arguments, "--repo-comments")
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
            if name == "add_comment":
                if self._source_control is None:
                    return {"ok": False, "error": "repository comments are not enabled"}
                pr_url, comment, file, line = _comment_arguments(arguments)
                try:
                    await self._source_control.add_comment(pr_url, comment, file, line)
                except Exception as error:
                    return {"ok": False, "error": f"could not add comment: {error}"}
                self._comments_added += 1
                return {"ok": True, "acknowledgement": "comment added"}
            if name == "clarify":
                if not isinstance(arguments, dict) or arguments:
                    return {
                        "ok": False,
                        "error": "clarify does not accept arguments",
                    }
                return {"ok": True, "acknowledgement": "clarified"}
            if name == "complete_step":
                if self._source_control is not None and not self._comments_added:
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


def _tools(repo_comments: bool = False) -> list[dict[str, object]]:
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
    if repo_comments:
        tools.append(
            {
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
            }
        )
    return tools


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
    host: str, port: int, token: str, *, repo_comments: bool = False
) -> None:
    """Serve newline-delimited MCP JSON-RPC without writing logs to stdout."""
    while line := await asyncio.to_thread(sys.stdin.buffer.readline):
        try:
            request: Any = json.loads(line)
            response = await _mcp_response(
                host, port, token, request, repo_comments=repo_comments
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
    repo_comments: bool = False,
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
        return _rpc_result(request_id, {"tools": _tools(repo_comments)})
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
    parser.add_argument("--repo-comments", action="store_true")
    args = parser.parse_args()
    asyncio.run(
        _serve_stdio(
            args.host, args.port, args.token, repo_comments=args.repo_comments
        )
    )


__all__ = [
    "TerminalEvent",
    "TerminalMcpBroker",
    "TerminalResultAlreadySubmittedError",
    "TerminalResultRegistry",
]
