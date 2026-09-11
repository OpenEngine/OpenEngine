"""Expose exactly one validated host callback to a provider CLI, over stdio MCP.

A surface that lets an agent reach back into the host does it the same way each
time: a loopback server in the host holding the callback, and a subprocess the
provider CLI speaks MCP to, relaying calls to it over a shared secret. Two
copies of that had already been written -- Slack's and the pull request's --
and they were identical down to the error strings.

What is *not* here is the part those two do differently. Each surface keeps its
own tool spec, its own validation, and its own answer, because those are its
authority: what an agent may ask for, and what it gets back. This module knows
only that there is one tool and how to carry a call to it, and it never looks
inside what a surface returns -- which is what makes it shareable without
making two surfaces' permissions move together.

Written for one tool deliberately. A broker that serves several needs to say
which grants are live for this run, and that is a question about authority
again; a surface that grows into it has outgrown this.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import sys
import tempfile
from abc import ABC, abstractmethod
from contextlib import suppress
from pathlib import Path
from typing import TextIO

McpRequestId = str | int

#: The MCP revision these servers speak. Reported as-is rather than echoing
#: what the client asked for: a version the server does not implement is not
#: made true by agreeing to it.
PROTOCOL_VERSION = "2025-06-18"


class SingleToolBroker(ABC):
    """A loopback server holding one tool, and the descriptor that reaches it.

    The half that lives in the host. Subclasses supply the tool -- its name, its
    schema, and `_submit`, which decides whether a call is allowed and what it
    returns -- while this carries the calls and holds the credential that
    authenticates them.
    """

    #: `python -m <this>` is the subprocess the provider CLI talks to. Named by
    #: the subclass because the tool spec it serves lives in its module.
    entry_point: str

    #: How the tool appears to the agent: `mcp__<server_name>__<tool>`.
    server_name: str

    def __init__(self) -> None:
        # Long enough that guessing is not a strategy, and never in argv --
        # anyone on this host can read that. It reaches the subprocess through
        # a file only this user can open.
        self._token = secrets.token_hex(32)
        self._server: asyncio.Server | None = None
        self._credential: TextIO | None = None

    async def __aenter__(self) -> SingleToolBroker:
        self._credential = tempfile.NamedTemporaryFile(mode="w", prefix="concierge-")
        self._credential.write(self._token)
        self._credential.flush()
        try:
            self._server = await asyncio.start_server(
                self._handle_connection, "127.0.0.1", 0
            )
        except BaseException:
            self._credential.close()
            raise
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        if self._credential is not None:
            self._credential.close()

    @property
    def config(self) -> dict[str, object]:
        """ACP stdio MCP descriptor; the credential never appears in argv."""
        if self._server is None or not self._server.sockets or self._credential is None:
            raise RuntimeError("concierge MCP broker has not been started")
        return {
            "name": self.server_name,
            "command": sys.executable,
            "args": ["-m", self.entry_point,
                     "--host", "127.0.0.1", "--port",
                     str(self._server.sockets[0].getsockname()[1]),
                     "--token-file", self._credential.name],
            "env": [],
        }

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

    @abstractmethod
    async def _submit(self, request: object) -> dict[str, object]:
        """Authorise and perform one call, as ``{"ok": ...}``.

        The surface's own authority, which is why it is not implemented here.
        Check `request["token"]` against `self._token` first: a connection to a
        loopback port is not by itself evidence of anything.
        """

    def _credentialled(self, request: object, tool_name: str) -> dict[str, object] | None:
        """The refusal a malformed or unauthenticated call deserves, if any.

        The part of `_submit` every surface shares, because it decides nothing
        about the surface: whoever is calling either holds this run's secret and
        named this run's tool, or is not talking to it at all.
        """
        if not isinstance(request, dict) or request.get("token") != self._token:
            return {"ok": False, "error": "invalid concierge credential"}
        name = request.get("name")
        if name != tool_name:
            return {"ok": False, "error": f"unknown concierge tool: {name}"}
        if not isinstance(request.get("arguments"), dict):
            return {"ok": False, "error": "arguments must be an object"}
        return None


# --- stdio MCP server (launched as a subprocess by the provider CLI) ---------


async def forward_call(
    host: str,
    port: int,
    token: str,
    request_id: McpRequestId,
    name: object,
    arguments: object,
) -> dict[str, object]:
    """Hand one call to the host that owns the tool, and wait for its answer.

    `name` and `arguments` go over unexamined. Judging them here would be
    judging them in the subprocess the agent's CLI started, which is the wrong
    side of the boundary; the broker is where the decision belongs.
    """
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


async def mcp_response(
    host: str,
    port: int,
    token: str,
    request: object,
    *,
    tool_spec: dict[str, object],
    server_info_name: str,
) -> dict[str, object] | None:
    """Answer one MCP request, or `None` for a notification, which gets none."""
    if not isinstance(request, dict):
        return rpc_error(None, -32600, "Invalid Request")
    request_id = request.get("id")
    method = request.get("method")
    if isinstance(method, str) and method.startswith("notifications/"):
        return None
    if method == "initialize":
        return rpc_result(
            request_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": server_info_name, "version": "1"},
            },
        )
    if method == "ping":
        return rpc_result(request_id, {})
    if method == "tools/list":
        return rpc_result(request_id, {"tools": [tool_spec]})
    if method != "tools/call":
        return rpc_error(request_id, -32601, "Method not found")
    if not isinstance(request_id, (str, int)) or isinstance(request_id, bool):
        return rpc_error(request_id, -32600, "Tool calls require a request id")
    params = request.get("params")
    if not isinstance(params, dict):
        return rpc_error(request_id, -32602, "Invalid tool parameters")
    forwarded = await forward_call(
        host,
        port,
        token,
        request_id,
        params.get("name"),
        params.get("arguments", {}),
    )
    if forwarded.get("ok") is not True:
        # A refusal is a result, not a protocol error: the agent asked a
        # well-formed question and the host said no, which it can act on.
        return rpc_result(
            request_id,
            {
                "content": [{"type": "text", "text": str(forwarded.get("error"))}],
                "isError": True,
            },
        )
    return rpc_result(
        request_id,
        {
            "content": [{"type": "text", "text": str(forwarded["text"])}],
            "structuredContent": forwarded.get("data", {}),
        },
    )


async def serve_stdio(
    host: str,
    port: int,
    token: str,
    *,
    tool_spec: dict[str, object],
    server_info_name: str,
) -> None:
    """Serve newline-delimited MCP JSON-RPC, writing nothing else to stdout."""
    while line := await asyncio.to_thread(sys.stdin.buffer.readline):
        try:
            response = await mcp_response(
                host, port, token, json.loads(line),
                tool_spec=tool_spec, server_info_name=server_info_name,
            )
            if response is None:
                continue
        except Exception as error:
            response = rpc_error(None, -32700, f"Parse error: {error}")
        sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
        sys.stdout.flush()


def rpc_result(request_id: object, result: object) -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def rpc_error(request_id: object, code: int, message: str) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def serve_from_command_line(
    *, tool_spec: dict[str, object], server_info_name: str
) -> None:
    """The `main()` of a broker's subprocess: parse where to call, then serve."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--token-file", required=True)
    arguments = parser.parse_args()
    asyncio.run(
        serve_stdio(
            arguments.host,
            arguments.port,
            Path(arguments.token_file).read_text(),
            tool_spec=tool_spec,
            server_info_name=server_info_name,
        )
    )


__all__ = [
    "PROTOCOL_VERSION",
    "McpRequestId",
    "SingleToolBroker",
    "forward_call",
    "mcp_response",
    "rpc_error",
    "rpc_result",
    "serve_from_command_line",
    "serve_stdio",
]
