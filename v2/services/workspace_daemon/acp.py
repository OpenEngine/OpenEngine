"""A small direct Agent Client Protocol (v1) client over an agent subprocess's stdio.

Newline-delimited JSON-RPC 2.0, one request in flight. No LangGraph or
langgraph-acp dependency. The client advertises no filesystem or terminal
capability, refuses every client method and denies every permission request
(approvals are not implemented). Writes and reads share each call's deadline,
so an agent that stops reading or writing cannot hold the executor past it.
Notifications that arrive while a session is loaded (its replayed history) are
discarded; only those during a prompt reach the caller as live progress.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from enum import StrEnum
from pathlib import Path
from typing import Any

MAX_FRAME = 1024 * 1024
PROTOCOL_VERSION = 1
READ_ONLY_MODE = "read-only"
REJECT_KINDS = ("reject_once", "reject_always")


class Violation(StrEnum):
    FRAME = "ACP frame exceeds the size limit or is not JSON"
    CLOSED = "ACP agent closed its output or sent an invalid envelope"
    FOREIGN = "ACP update belongs to another session"
    IDENTITY = "ACP response identity mismatch"
    REJECTED = "ACP agent rejected the request"
    VERSION = "ACP protocol version mismatch"
    UNLOADABLE = "ACP agent cannot load the existing conversation"
    SESSION = "ACP agent returned an invalid session identity"
    WRITABLE = "ACP agent does not offer a read-only mode"
    MODEL = "ACP agent does not offer the requested model"


class ACPError(RuntimeError):
    """The agent broke the protocol, refused a request or lacks a required capability."""

    def __init__(self, reason: Violation, method: str | None = None) -> None:
        super().__init__(reason if method is None else f"{reason}: {method}")
        self.reason = reason


Update = Callable[[dict[str, Any]], Awaitable[None]]


class ACPClient:
    def __init__(self, process, *, timeout: float = 30.0) -> None:
        self.process, self.timeout = process, timeout
        self.next_id = 0
        self.session_id: str | None = None
        # Drained, never stored: agent diagnostics can hold secrets.
        self._stderr = asyncio.create_task(self._discard(process.stderr))

    @staticmethod
    async def _discard(stream) -> None:
        while await stream.read(65536):
            pass

    async def _send(self, value: dict[str, Any]) -> None:
        wire = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode() + b"\n"
        if len(wire) > MAX_FRAME:
            raise ACPError(Violation.FRAME)
        self.process.stdin.write(wire)
        await self.process.stdin.drain()

    async def call(self, method: str, params: dict[str, Any], *, update: Update | None = None,
                   timeout: float | None = None) -> dict[str, Any]:
        self.next_id += 1
        request_id = self.next_id
        async with asyncio.timeout(timeout or self.timeout):
            await self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
            while True:
                try:
                    line = await self.process.stdout.readline()
                    message = json.loads(line) if line else None
                except ValueError:  # over the stream limit, or not JSON
                    raise ACPError(Violation.FRAME) from None
                if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                    raise ACPError(Violation.CLOSED)
                if "method" in message:
                    if "id" in message:
                        await self._answer(message)
                    elif message["method"] == "session/update" and update is not None:
                        notice = message.get("params")
                        if not isinstance(notice, dict) or notice.get("sessionId") != self.session_id:
                            raise ACPError(Violation.FOREIGN)
                        if isinstance(notice.get("update"), dict):
                            await update(notice["update"])
                    continue
                if message.get("id") != request_id:
                    raise ACPError(Violation.IDENTITY)
                if "error" in message or not isinstance(message.get("result"), dict):
                    raise ACPError(Violation.REJECTED, method)
                return message["result"]

    async def _answer(self, message: dict[str, Any]) -> None:
        reply: dict[str, Any] = {"jsonrpc": "2.0", "id": message["id"]}
        if message["method"] == "session/request_permission":
            options = (message.get("params") or {}).get("options") or []
            reject = next((option["optionId"] for kind in REJECT_KINDS for option in options
                           if isinstance(option, dict) and option.get("kind") == kind and "optionId" in option), None)
            reply["result"] = {"outcome": {"outcome": "selected", "optionId": reject} if reject else {"outcome": "cancelled"}}
        else:
            reply["error"] = {"code": -32601, "message": "client method is not supported"}
        await self._send(reply)

    async def open(self, cwd: Path, *, previous: str | None = None, model: str | None = None) -> tuple[str, bool]:
        """Start or load a conversation in read-only mode; returns its identity and whether it can be loaded later."""
        initialized = await self.call("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False},
            "clientInfo": {"name": "openengine-workspace-daemon", "version": "0.0.0"}})
        if initialized.get("protocolVersion") != PROTOCOL_VERSION:
            raise ACPError(Violation.VERSION)
        resumable = (initialized.get("agentCapabilities") or {}).get("loadSession") is True
        params = {"cwd": str(cwd), "mcpServers": []}
        if previous is not None:
            if not resumable:
                raise ACPError(Violation.UNLOADABLE)
            # No update handler: the replayed history is not live progress.
            result = await self.call("session/load", {**params, "sessionId": previous})
            self.session_id = previous
        else:
            result = await self.call("session/new", params)
            self.session_id = result.get("sessionId")
        if not isinstance(self.session_id, str) or not 0 < len(self.session_id) <= 1024:
            raise ACPError(Violation.SESSION)
        modes = (result.get("modes") or {}).get("availableModes") or []
        if not any(isinstance(mode, dict) and mode.get("id") == READ_ONLY_MODE for mode in modes):
            raise ACPError(Violation.WRITABLE)
        await self.call("session/set_mode", {"sessionId": self.session_id, "modeId": READ_ONLY_MODE})
        if model is not None:
            await self._select_model(result.get("configOptions") or [], model)
        return self.session_id, resumable

    async def _select_model(self, options: list[Any], model: str) -> None:
        option = next((item for item in options if isinstance(item, dict) and item.get("category") == "model"), None)
        values = {choice.get("value") for choice in (option or {}).get("options") or [] if isinstance(choice, dict)}
        if option is None or model not in values:
            raise ACPError(Violation.MODEL)
        await self.call("session/set_config_option", {"sessionId": self.session_id, "configId": option["id"], "value": model})

    async def prompt(self, text: str, update: Update, timeout: float) -> dict[str, Any]:
        return await self.call("session/prompt", {"sessionId": self.session_id, "prompt": [{"type": "text", "text": text}]},
                               update=update, timeout=timeout)

    async def close(self) -> None:
        """Release the pipes once the process tree is fenced; unread output is drained so the process can be reaped."""
        try:
            self.process.stdin.close()
            async with asyncio.timeout(5):
                await asyncio.gather(self._discard(self.process.stdout), self.process.wait(), self._stderr)
        except (OSError, TimeoutError):
            self._stderr.cancel()
