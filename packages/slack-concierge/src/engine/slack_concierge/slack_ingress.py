"""Verified Slack payloads enter here; HTTP signature verification stays at the edge."""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
import re
from collections import OrderedDict

from starlette.requests import Request
from starlette.responses import Response, JSONResponse

from engine.domain import RunOrigin
from .slack_concierge import IncomingMessage, SlackConcierge

log = logging.getLogger(__name__)


class SlackIngress:
    """Bounded background queue, deduplicated by message identity across event types."""

    def __init__(self, concierge: SlackConcierge, *, capacity: int = 256,
                 signing_secret: Callable[[], str] = lambda: "",
                 verify_signature: Callable[[str, str, str, bytes], bool] = lambda *args: False,
                 connected: Callable[[], bool] = lambda: True,
                 react: Callable[[str, str, str], Awaitable[None]] | None = None) -> None:
        self._signing_secret = signing_secret
        self._verify_signature = verify_signature
        self._connected = connected
        self._react = react
        self.concierge = concierge
        self._queue: asyncio.Queue[IncomingMessage] = asyncio.Queue(maxsize=capacity)
        self._seen: OrderedDict[tuple[str, str], None] = OrderedDict()
        self._worker: asyncio.Task[None] | None = None
        self._pending: set[tuple[str, str]] = set()

    async def webhook(self, request: Request) -> Response:
        """Authenticate, enqueue, and acknowledge without waiting for an agent."""
        body = await request.body()
        signing_secret = self._signing_secret()
        if not signing_secret:
            log.warning(
                "a Slack event was delivered but no signing secret is saved, "
                "so it could not be verified and was ignored"
            )
            return JSONResponse({"error": "Slack request signing is not configured"}, status_code=503)
        if not self._verify_signature(
            signing_secret,
            request.headers.get("x-slack-request-timestamp", ""),
            request.headers.get("x-slack-signature", ""),
            body,
        ):
            return JSONResponse({"error": "invalid Slack signature"}, status_code=401)
        try:
            payload = json.loads(body)
        except ValueError:
            return JSONResponse({"error": "invalid Slack event"}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse({"error": "invalid Slack event"}, status_code=400)
        if payload.get("type") == "url_verification":
            # The one-off handshake that makes Slack accept this address.
            return JSONResponse({"challenge": str(payload.get("challenge", ""))})
        if not self._connected():
            log.warning("Slack event ignored: no bot token is configured")
            return Response(status_code=200)
        known_thread = False
        event = payload.get("event")
        if (
            isinstance(event, dict)
            and event.get("type") == "message"
            and not event.get("bot_id")
            and not event.get("subtype")
        ):
            channel, thread = event.get("channel"), event.get("thread_ts")
            if isinstance(channel, str) and isinstance(thread, str) and channel and thread:
                known_thread = bool(await self.concierge.linked_workorders(
                    RunOrigin(channel=channel, thread_id=thread)
                ))
        accepted = self.accept(payload, known_thread=known_thread)
        return Response(status_code=200 if accepted else 503)

    def accept(self, payload: dict, *, known_thread: bool = False) -> bool:
        event = payload.get("event")
        if payload.get("type") != "event_callback" or not isinstance(event, dict):
            return True
        kind = event.get("type")
        if kind not in ("app_mention", "message") or event.get("bot_id") or event.get("subtype"):
            return True
        channel, author, ts = (event.get(k) for k in ("channel", "user", "ts"))
        if not all(isinstance(x, str) and x for x in (channel, author, ts)):
            return True
        thread = event.get("thread_ts") or ts
        if not isinstance(thread, str):
            return True
        key = (channel, thread)
        if kind == "message" and not (known_thread or self.concierge.has_thread(*key) or key in self._pending):
            return True
        identity = (channel, ts)
        if identity in self._seen:
            return True
        if self._queue.full():
            return False
        raw_text = str(event.get("text", ""))
        text = re.sub(r"<@[^>]+>", "", raw_text).strip()
        message = IncomingMessage(
            RunOrigin(channel=channel, thread_id=thread, author=author),
            text or "Hello", message_ts=ts, raw_text=raw_text,
            mentioned_users=tuple(re.findall(r"<@([^>|]+)(?:\|[^>]+)?>", raw_text)),
            event_type=kind,
        )
        self._queue.put_nowait(message)
        self._pending.add(key)
        self._seen[identity] = None
        while len(self._seen) > 4096:
            self._seen.popitem(last=False)
        if self._worker is None:
            self._worker = asyncio.create_task(self._run())
        return True

    async def _run(self) -> None:
        while True:
            message = await self._queue.get()
            try:
                if self._react and message.message_ts:
                    try:
                        await self._react(message.origin.channel, message.message_ts, "eyes")
                    except Exception:
                        log.exception("Could not add eyes reaction")
                await self.concierge.handle(message)
            except Exception:
                log.exception("Slack concierge turn failed")
                try:
                    await self.concierge.reply(message.origin, "Sorry, something went wrong. Please try again.")
                except Exception:
                    log.exception("Could not post Slack error reply")
            finally:
                self._pending.discard((message.origin.channel, message.origin.thread_id))
                self._queue.task_done()

    async def drain(self) -> None:
        await self._queue.join()

    async def close(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
        await self.concierge.close()
