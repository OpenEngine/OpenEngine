"""JSON-RPC 2.0 over a newline-delimited byte stream.

ACP is JSON-RPC 2.0, one message per line, spoken over a child process's stdin
and stdout. That is a small enough protocol to implement here, and implementing
it here is what lets this package keep an empty dependency list while still
talking to a real agent.

The peer knows nothing about ACP. It correlates requests with responses,
delivers notifications, answers incoming requests through a callback, and
reports the one failure a pipe has: the far side stopped talking. Everything
about sessions, prompts, and capabilities lives a layer up.

Both directions carry requests. The agent asks the client for permission, for
file contents, for a terminal; a peer that could only call outwards would be
half a connection.
"""

import asyncio
import json
from collections.abc import Awaitable, Callable
from itertools import count

from langgraph_acp._json import JSONObject, JSONValue

#: The JSON-RPC error codes this peer sends, from the specification's list.
METHOD_NOT_FOUND = -32601
INTERNAL_ERROR = -32603


class JSONRPCError(Exception):
    """An error *response*: the far side answered, and the answer was a refusal.

    Distinct from a transport failure. This means the agent is alive and
    declined; the caller above translates it into whichever `ACPError` describes
    the operation that was refused.
    """

    def __init__(self, code: int, message: str, data: JSONValue = None) -> None:
        super().__init__(f"{message} (code {code})")
        self.code = code
        self.message = message
        self.data = data

    def as_response_error(self) -> JSONObject:
        error: JSONObject = {"code": self.code, "message": self.message}
        if self.data is not None:
            error["data"] = self.data
        return error


class JSONRPCPeer:
    """One end of a JSON-RPC connection over a pair of asyncio streams."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        on_request: Callable[[str, JSONObject], Awaitable[JSONValue]],
        on_notification: Callable[[str, JSONObject], None],
        on_closed: Callable[[], BaseException],
        on_eof: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._on_request = on_request
        self._on_notification = on_notification
        self._on_closed = on_closed
        self._on_eof = on_eof
        self._pending: dict[int, asyncio.Future[JSONValue]] = {}
        self._ids = count()
        self._answering: set[asyncio.Task[None]] = set()
        self._reading: asyncio.Task[None] | None = None
        self._closed = False

    def start(self) -> None:
        """Begin reading. Nothing is delivered or correlated until this runs."""
        if self._reading is None:
            self._reading = asyncio.create_task(self._read_until_eof())

    async def request(self, method: str, params: JSONObject) -> JSONValue:
        """Call the far side and wait for its answer.

        Raises `JSONRPCError` if the answer is an error response, or whatever
        `on_closed` produces if the connection ends before one arrives.
        """
        if self._closed:
            raise self._on_closed()
        message_id = next(self._ids)
        future: asyncio.Future[JSONValue] = asyncio.get_running_loop().create_future()
        self._pending[message_id] = future
        try:
            await self._send(
                {
                    "jsonrpc": "2.0",
                    "id": message_id,
                    "method": method,
                    "params": params,
                }
            )
            return await future
        finally:
            # Popped on every exit, cancellation included: an abandoned turn must
            # not leave an entry that a late response would resolve.
            self._pending.pop(message_id, None)

    async def notify(self, method: str, params: JSONObject) -> None:
        """Tell the far side something. There is no answer to wait for."""
        await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def notify_nowait(self, method: str, params: JSONObject) -> None:
        """Queue a notification without awaiting the write.

        For the one case that cannot await: unwinding a cancelled turn, where
        every suspension point is another chance to be cancelled again. A
        notification is a few hundred bytes and the transport buffers it, so the
        write itself does not block; only backpressure would, and this skips it.
        """
        try:
            self._write({"jsonrpc": "2.0", "method": method, "params": params})
        except (ConnectionError, RuntimeError):
            # The pipe is already gone, which is the outcome this was asking for.
            pass

    async def aclose(self) -> None:
        """Stop reading, close the write side, and fail anything still pending."""
        self._closed = True
        if self._reading is not None:
            self._reading.cancel()
            self._reading = None
        for task in tuple(self._answering):
            task.cancel()
        self._fail_pending(None)
        if not self._writer.is_closing():
            self._writer.close()
        try:
            await self._writer.wait_closed()
        except (ConnectionError, BrokenPipeError):
            pass

    def _write(self, message: JSONObject) -> None:
        self._writer.write(json.dumps(message).encode() + b"\n")

    async def _send(self, message: JSONObject) -> None:
        if self._closed or self._writer.is_closing():
            raise self._on_closed()
        try:
            self._write(message)
            await self._writer.drain()
        except (ConnectionError, RuntimeError) as exc:
            raise self._on_closed() from exc

    async def _read_until_eof(self) -> None:
        try:
            await self._read()
        except Exception:
            # A transport failure and a clean EOF are the same event to a caller
            # waiting on a request: no answer is coming. `on_closed` describes
            # both, and it can see the exit status and stderr that explain it.
            pass
        finally:
            self._closed = True
        # Only on the natural path. Cancellation means `aclose` is already
        # unwinding this connection and will fail what is pending itself.
        if self._on_eof is not None:
            await self._on_eof()
        self._fail_pending(None)

    async def _read(self) -> None:
        while True:
            line = await self._reader.readline()
            if not line:
                return
            stripped = line.strip()
            if not stripped:
                continue
            try:
                message = json.loads(stripped)
            except json.JSONDecodeError:
                # Agents write to stdout for reasons other than the protocol.
                # A line that is not a message cannot be answered or correlated,
                # and killing the connection over it would be a worse answer.
                continue
            if isinstance(message, dict):
                self._dispatch(message)

    def _dispatch(self, message: dict[str, JSONValue]) -> None:
        method = message.get("method")
        if isinstance(method, str):
            raw_params = message.get("params")
            params: JSONObject = dict(raw_params) if isinstance(raw_params, dict) else {}
            message_id = message.get("id")
            if message_id is None:
                self._on_notification(method, params)
            else:
                task = asyncio.create_task(self._answer(message_id, method, params))
                self._answering.add(task)
                task.add_done_callback(self._answering.discard)
            return
        self._resolve(message)

    def _resolve(self, message: dict[str, JSONValue]) -> None:
        message_id = message.get("id")
        if not isinstance(message_id, int) or isinstance(message_id, bool):
            return
        future = self._pending.pop(message_id, None)
        if future is None or future.done():
            return
        error = message.get("error")
        if isinstance(error, dict):
            code = error.get("code")
            detail = error.get("message")
            future.set_exception(
                JSONRPCError(
                    code if isinstance(code, int) else INTERNAL_ERROR,
                    detail if isinstance(detail, str) else "the agent reported an error",
                    error.get("data"),
                )
            )
            return
        future.set_result(message.get("result"))

    async def _answer(self, message_id: JSONValue, method: str, params: JSONObject) -> None:
        try:
            result = await self._on_request(method, params)
        except asyncio.CancelledError:
            raise
        except JSONRPCError as exc:
            reply: JSONObject = {"error": exc.as_response_error()}
        except Exception as exc:
            reply = {"error": {"code": INTERNAL_ERROR, "message": str(exc)}}
        else:
            reply = {"result": result}
        try:
            await self._send({"jsonrpc": "2.0", "id": message_id, **reply})
        except Exception:
            # Nothing is listening for this answer any more, and the request that
            # provoked it has already failed on its own account.
            pass

    def _fail_pending(self, reason: BaseException | None) -> None:
        for future in tuple(self._pending.values()):
            if not future.done():
                future.set_exception(reason or self._on_closed())
        self._pending.clear()


__all__ = ["INTERNAL_ERROR", "METHOD_NOT_FOUND", "JSONRPCError", "JSONRPCPeer"]
