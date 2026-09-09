"""Concierge MCP broker for Slack thread conversations.

The concierge is a lightweight agent that greets users mentioning the bot
and can start work orders on their behalf.  It runs outside the workflow
system -- there is no step to complete, no run to drive -- so it gets its
own broker that serves only ``create_workorder``.

The broker follows the same TCP bridge pattern as ``PlanningMcpBroker``:
the provider CLI launches a stdio MCP server that forwards tool calls
back to this process over a local TCP connection.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import sys
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress

from engine.ports import McpServerConfig

#: Given (repository, prompt) create a work order and return (url, run_id).
CreateWorkorder = Callable[[str, str], Awaitable[tuple[str, str]]]

McpRequestId = str | int
_PROTOCOL_VERSION = "2025-06-18"
_SERVER_NAME = "concierge"

CONCIERGE_TOOL_NAME = "create_workorder"

_TOOL_SPEC: dict[str, object] = {
    "name": CONCIERGE_TOOL_NAME,
    "description": (
        "Start a new work order. The work order runs a workflow that "
        "implements the requested task, and status updates will be posted "
        "in this conversation. Returns the work order URL so you can share "
        "it with the user."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "minLength": 1,
                "description": "What the work order should accomplish.",
            },
            "repository": {
                "type": "string",
                "description": (
                    "The repository to work in, as owner/name. "
                    "Optional when a default is configured."
                ),
            },
        },
        "required": ["prompt"],
        "additionalProperties": False,
    },
}


class ConciergeBroker:
    """Expose ``create_workorder`` to a provider CLI over a local MCP server.

    The factory that creates it binds the callback that actually starts the
    work order, so the broker itself has no opinion about workflows, runners,
    or repositories -- it validates the call, forwards it, and returns the
    answer.
    """

    def __init__(
        self,
        *,
        create_workorder: CreateWorkorder,
        default_repository: str = "",
    ) -> None:
        self._create_workorder = create_workorder
        self._default_repository = default_repository
        self._token = secrets.token_hex(32)
        self._server: asyncio.Server | None = None

    async def __aenter__(self) -> ConciergeBroker:
        self._server = await asyncio.start_server(
            self._handle_connection, "127.0.0.1", 0
        )
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    @property
    def config(self) -> McpServerConfig:
        if self._server is None or not self._server.sockets:
            raise RuntimeError("concierge MCP broker has not been started")
        port = self._server.sockets[0].getsockname()[1]
        return McpServerConfig(
            name=_SERVER_NAME,
            command=sys.executable,
            args=(
                "-m",
                "engine.runtime.concierge_mcp_server",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--token",
                self._token,
            ),
        )

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            request = json.loads(await reader.readline())
            response = await self._submit(request)
        except Exception as error:
            response = {"ok": False, "error": f"invalid concierge request: {error}"}
        writer.write(json.dumps(response, separators=(",", ":")).encode() + b"\n")
        with suppress(ConnectionError):
            await writer.drain()
        writer.close()
        with suppress(ConnectionError):
            await writer.wait_closed()

    async def _submit(self, request: object) -> dict[str, object]:
        if not isinstance(request, dict) or request.get("token") != self._token:
            return {"ok": False, "error": "invalid concierge credential"}
        name = request.get("name")
        arguments = request.get("arguments")
        if name != CONCIERGE_TOOL_NAME:
            return {"ok": False, "error": f"unknown concierge tool: {name}"}
        if not isinstance(arguments, dict):
            return {"ok": False, "error": "arguments must be an object"}
        prompt = arguments.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            return {"ok": False, "error": "prompt must be a non-empty string"}
        repository = str(arguments.get("repository", "") or self._default_repository)
        if not repository:
            return {
                "ok": False,
                "error": (
                    "repository is required because no default is configured; "
                    "ask the user which repository to work in"
                ),
            }
        try:
            url, run_id = await self._create_workorder(repository, prompt.strip())
        except Exception as error:
            return {"ok": False, "error": f"could not start the work order: {error}"}
        return {
            "ok": True,
            "text": (
                f"Work order `{run_id}` started on `{repository}`. "
                "Status updates will appear in this thread."
            ),
            "data": {"run_id": run_id, "url": url, "repository": repository},
        }


# --- stdio MCP server (launched as a subprocess by the provider CLI) ---------


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


async def _mcp_response(
    host: str,
    port: int,
    token: str,
    request: object,
) -> dict[str, object] | None:
    if not isinstance(request, dict):
        return _rpc_error(None, -32600, "Invalid Request")
    request_id = request.get("id")
    method = request.get("method")
    if isinstance(method, str) and method.startswith("notifications/"):
        return None
    if method == "initialize":
        params = request.get("params")
        protocol = (
            params.get("protocolVersion", _PROTOCOL_VERSION)
            if isinstance(params, dict)
            else _PROTOCOL_VERSION
        )
        return _rpc_result(
            request_id,
            {
                "protocolVersion": protocol,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "engine-concierge", "version": "1"},
            },
        )
    if method == "ping":
        return _rpc_result(request_id, {})
    if method == "tools/list":
        return _rpc_result(request_id, {"tools": [_TOOL_SPEC]})
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
    if forwarded.get("ok") is not True:
        return _rpc_result(
            request_id,
            {
                "content": [{"type": "text", "text": str(forwarded.get("error"))}],
                "isError": True,
            },
        )
    return _rpc_result(
        request_id,
        {
            "content": [{"type": "text", "text": str(forwarded["text"])}],
            "structuredContent": forwarded.get("data", {}),
        },
    )


async def _serve_stdio(host: str, port: int, token: str) -> None:
    while line := await asyncio.to_thread(sys.stdin.buffer.readline):
        try:
            response = await _mcp_response(host, port, token, json.loads(line))
            if response is None:
                continue
        except Exception as error:
            response = _rpc_error(None, -32700, f"Parse error: {error}")
        sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
        sys.stdout.flush()


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
    arguments = parser.parse_args()
    asyncio.run(_serve_stdio(arguments.host, arguments.port, arguments.token))


__all__ = [
    "CONCIERGE_TOOL_NAME",
    "ConciergeBroker",
]
