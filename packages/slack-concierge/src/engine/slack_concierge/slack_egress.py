"""Outbound work-order tool: validated host callback exposed through stdio MCP.

The host binds the Slack origin and posts status and links through its notifier.
The agent supplies only a repository and task; it cannot redirect those replies.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import sys
import tempfile
from pathlib import Path
from typing import TextIO
from langgraph_acp.permissions import ACPPermissionRequest, ACPPermissionOutcome
from collections.abc import Awaitable, Callable
from contextlib import suppress



#: Given (repository, prompt) create a work order and return (url, run_id).
CreateWorkorder = Callable[[str, str], Awaitable[tuple[str, str]]]
SteerWorkorder = Callable[[str], Awaitable[tuple[str, str]]]
AnswerQuestion = Callable[[str, dict[str, list[str]]], Awaitable[tuple[str, str]]]
DecideReview = Callable[[bool, str], Awaitable[tuple[str, str]]]

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
        },
        "required": ["prompt"],
        "additionalProperties": False,
    },
}

_STEER_TOOL_SPEC = {
    **_TOOL_SPEC,
    "name": "steer_workorder",
    "description": (
        "Send new instructions to the running work order in this Slack thread. "
        "Preserves its work and conversation. Does not create a new work order, "
        "reopen completed work, or approve a pending decision. The host selects "
        "the linked work order; ambiguous or non-editable work is refused."
    ),
}

_RESUME_TOOL_SPEC = {
    **_TOOL_SPEC,
    "name": "resume_workorder",
    "description": (
        "Continue the existing work order in this Slack thread after it finishes, "
        "fails or awaits review. Use for follow-up fixes such as failing "
        "tests. Retains the existing work order and history. Does not create new "
        "work or approve a decision. The host selects a unique editable implementation."
    ),
}

_ANSWER_TOOL_SPEC = {
    "name": "answer_workorder_question",
    "description": (
        "Submit the human's answers to a pending structured question shown in host context. "
        "Use the exact approval_id and question IDs. Never invent answers; ask the human "
        "if their reply is ambiguous or incomplete. This cannot grant tool permissions or approve reviews."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "approval_id": {"type": "string", "minLength": 1},
            "answers": {"type": "object", "minProperties": 1, "additionalProperties": {
                "type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1},
            }},
        },
        "required": ["approval_id", "answers"], "additionalProperties": False,
    },
}

_REVIEW_TOOL_SPEC = {
    "name": "decide_workorder_review",
    "description": (
        "Submit an explicit approval or request for changes for the pending human review "
        "in this Slack thread. The host chooses the linked WorkOrder. Set approved true only "
        "when the user clearly approves; set it false only when they clearly request changes, "
        "putting their feedback in summary. Never infer a decision from a question or status check."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "approved": {"type": "boolean"},
            "summary": {"type": "string"},
        },
        "required": ["approved"],
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
        steer_workorder: SteerWorkorder | None = None,
        resume_workorder: SteerWorkorder | None = None,
        answer_question: AnswerQuestion | None = None,
        decide_review: DecideReview | None = None,
    ) -> None:
        self._create_workorder = create_workorder
        self._steer_workorder = steer_workorder
        self._resume_workorder = resume_workorder
        self._answer_question = answer_question
        self._decide_review = decide_review
        self._default_repository = default_repository
        self._token = secrets.token_hex(32)
        self._server: asyncio.Server | None = None
        self._credential: TextIO | None = None

    async def __aenter__(self) -> ConciergeBroker:
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
            "name": _SERVER_NAME,
            "command": sys.executable,
            "args": ["-m", "engine.slack_concierge.slack_egress",
                     "--host", "127.0.0.1", "--port",
                     str(self._server.sockets[0].getsockname()[1]),
                     "--token-file", self._credential.name,
                     *(["--enable-steering"] if self._steer_workorder else []),
                     *(["--enable-resuming"] if self._resume_workorder else []),
                     *(["--enable-answers"] if self._answer_question else []),
                     *(["--enable-review-decisions"] if self._decide_review else [])],
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

    async def _submit(self, request: object) -> dict[str, object]:
        if not isinstance(request, dict) or request.get("token") != self._token:
            return {"ok": False, "error": "invalid concierge credential"}
        name = request.get("name")
        arguments = request.get("arguments")
        if name == "decide_workorder_review":
            if self._decide_review is None:
                return {"ok": False, "error": "review decisions are not enabled"}
            if not isinstance(arguments, dict) or set(arguments) - {"approved", "summary"}:
                return {"ok": False, "error": "provide approved and optional summary only"}
            approved = arguments.get("approved")
            summary = arguments.get("summary", "")
            if not isinstance(approved, bool) or not isinstance(summary, str):
                return {"ok": False, "error": "approved must be a boolean and summary must be a string"}
            if not approved and not summary.strip():
                return {"ok": False, "error": "a request for changes needs feedback"}
            try:
                url, run_id = await self._decide_review(approved, summary.strip())
            except Exception as error:
                return {"ok": False, "error": f"could not submit the review decision: {error}"}
            outcome = "approved" if approved else "changes requested"
            return {"ok": True, "text": f"Review {outcome} for work order `{run_id}`.",
                    "data": {"run_id": run_id, "url": url, "approved": approved}}
        if name == "answer_workorder_question":
            if self._answer_question is None:
                return {"ok": False, "error": "answering questions is not enabled"}
            if not isinstance(arguments, dict) or set(arguments) != {"approval_id", "answers"}:
                return {"ok": False, "error": "provide approval_id and answers only"}
            approval_id, answers = arguments["approval_id"], arguments["answers"]
            if not isinstance(approval_id, str) or not approval_id.strip():
                return {"ok": False, "error": "approval_id must be a non-empty string"}
            if not isinstance(answers, dict) or not answers or any(
                not isinstance(key, str) or not key.strip()
                or not isinstance(values, list) or not values
                or any(not isinstance(value, str) or not value.strip() for value in values)
                for key, values in answers.items()
            ):
                return {"ok": False, "error": "answers must map question IDs to non-empty arrays of strings"}
            try:
                url, run_id = await self._answer_question(approval_id, answers)
            except Exception as error:
                return {"ok": False, "error": f"could not answer the question: {error}"}
            return {"ok": True, "text": f"Answer recorded for work order `{run_id}` and delivered to the waiting agent.",
                    "data": {"run_id": run_id, "url": url}}
        if name not in (CONCIERGE_TOOL_NAME, "steer_workorder", "resume_workorder"):
            return {"ok": False, "error": f"unknown concierge tool: {name}"}
        if not isinstance(arguments, dict):
            return {"ok": False, "error": "arguments must be an object"}
        prompt = arguments.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            return {"ok": False, "error": "prompt must be a non-empty string"}
        if set(arguments) - {"prompt"}:
            return {"ok": False, "error": "unknown work-order arguments"}
        if name in ("steer_workorder", "resume_workorder"):
            action = "steer" if name == "steer_workorder" else "resume"
            callback = self._steer_workorder if action == "steer" else self._resume_workorder
            if callback is None:
                return {"ok": False, "error": f"{action} is not enabled"}
            try:
                url, run_id = await callback(prompt.strip())
            except Exception as error:
                return {"ok": False, "error": f"could not {action} the work order: {error}"}
            return {
                "ok": True,
                "text": f"Instruction delivered to work order `{run_id}`. This does not mean the change is implemented yet.",
                "data": {"run_id": run_id, "url": url},
            }
        repository = self._default_repository or "."
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
    *,
    steer_enabled: bool = False,
    resume_enabled: bool = False,
    answers_enabled: bool = False,
    review_decisions_enabled: bool = False,
) -> dict[str, object] | None:
    if not isinstance(request, dict):
        return _rpc_error(None, -32600, "Invalid Request")
    request_id = request.get("id")
    method = request.get("method")
    if isinstance(method, str) and method.startswith("notifications/"):
        return None
    if method == "initialize":
        return _rpc_result(
            request_id,
            {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "engine-concierge", "version": "1"},
            },
        )
    if method == "ping":
        return _rpc_result(request_id, {})
    if method == "tools/list":
        return _rpc_result(request_id, {"tools": [
            _TOOL_SPEC, *([_STEER_TOOL_SPEC] if steer_enabled else []),
            *([_RESUME_TOOL_SPEC] if resume_enabled else []),
            *([_ANSWER_TOOL_SPEC] if answers_enabled else []),
            *([_REVIEW_TOOL_SPEC] if review_decisions_enabled else []),
        ]})
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


async def _serve_stdio(host: str, port: int, token: str, *, steer_enabled: bool = False, resume_enabled: bool = False, answers_enabled: bool = False, review_decisions_enabled: bool = False) -> None:
    while line := await asyncio.to_thread(sys.stdin.buffer.readline):
        try:
            response = await _mcp_response(host, port, token, json.loads(line), steer_enabled=steer_enabled, resume_enabled=resume_enabled, answers_enabled=answers_enabled, review_decisions_enabled=review_decisions_enabled)
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
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--enable-steering", action="store_true")
    parser.add_argument("--enable-resuming", action="store_true")
    parser.add_argument("--enable-answers", action="store_true")
    parser.add_argument("--enable-review-decisions", action="store_true")
    arguments = parser.parse_args()
    asyncio.run(_serve_stdio(arguments.host, arguments.port, Path(arguments.token_file).read_text(), steer_enabled=arguments.enable_steering, resume_enabled=arguments.enable_resuming, answers_enabled=arguments.enable_answers, review_decisions_enabled=arguments.enable_review_decisions))


__all__ = [
    "CONCIERGE_TOOL_NAME",
    "ConciergeBroker",
]


async def tool_permission(request: ACPPermissionRequest) -> ACPPermissionOutcome:
    """Approve only the named concierge MCP grants."""

    names = {f"{prefix}{name}" for prefix in ("mcp__concierge__", "concierge/")
             for name in ("create_workorder", "steer_workorder", "resume_workorder", "answer_workorder_question", "decide_workorder_review")}
    if any(isinstance(value, str) and value in names
           for value in (request.tool_call.get(field) for field in ("name", "toolName", "title"))):
        for option in request.options:
            if option.kind == "allow_once":
                return ACPPermissionOutcome.selected(option.option_id)
    return ACPPermissionOutcome.cancelled()


if __name__ == "__main__":
    main()
