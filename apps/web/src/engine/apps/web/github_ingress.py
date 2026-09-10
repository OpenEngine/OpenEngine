"""Verified GitHub deliveries enter here; the agent that answers runs behind a queue.

The GitHub counterpart of `slack_ingress.py`. GitHub gives a webhook ten
seconds and retries a delivery that took longer, so the route authenticates,
enqueues, and acknowledges rather than waiting for the work: a reply that
arrived late would otherwise be indistinguishable from a second copy of the
same comment.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

log = logging.getLogger(__name__)

#: The delivery kinds this route acts on. Anything else is acknowledged and
#: dropped -- a GitHub app subscribed to more events than Engine reads is a
#: configuration this route tolerates rather than an error it reports.
HANDLED_EVENTS = frozenset({"issue_comment", "pull_request_review_comment"})

#: How many comment identities are remembered for deduplication.
_SEEN_LIMIT = 4096


@dataclass(frozen=True)
class GithubComment:
    """One comment on an issue or a pull request, as this process reads it."""

    comment_id: str
    repository: str
    number: int
    author: str
    body: str
    url: str
    event: str
    is_pull_request: bool = False
    #: The review comment this one answers, for a reply inside a review thread.
    in_reply_to_id: str = ""


def verify_signature(webhook_secret: str, signature: str, body: bytes) -> bool:
    """Whether GitHub signed this delivery with the configured webhook secret.

    Unlike Slack, GitHub signs the body alone: there is no timestamp to age out
    a replay, so a repeated delivery is caught by the comment id rather than
    here.
    """
    if not webhook_secret or not signature:
        return False
    expected = "sha256=" + hmac.new(webhook_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def comment_from_payload(event: str, payload: Mapping[str, object]) -> GithubComment | None:
    """The comment in a delivery, or ``None`` for anything not worth an agent.

    Edits and deletions are excluded with everything else: only a new comment
    is somebody asking for something.
    """
    if event not in HANDLED_EVENTS or payload.get("action") != "created":
        return None
    comment = payload.get("comment")
    repository = payload.get("repository")
    subject = payload.get("issue") if event == "issue_comment" else payload.get("pull_request")
    if not isinstance(comment, dict) or not isinstance(repository, dict) or not isinstance(subject, dict):
        return None
    user = comment.get("user")
    if not isinstance(user, dict) or user.get("type") == "Bot":
        # A bot's comment includes this process's own replies, and answering
        # those is how a webhook talks to itself forever.
        return None
    author, full_name = user.get("login"), repository.get("full_name")
    number, comment_id = subject.get("number"), comment.get("id")
    if not isinstance(author, str) or not author or not isinstance(full_name, str) or not full_name:
        return None
    if not isinstance(number, int) or isinstance(number, bool):
        return None
    if not isinstance(comment_id, (int, str)) or isinstance(comment_id, bool) or comment_id == "":
        return None
    in_reply_to = comment.get("in_reply_to_id")
    return GithubComment(
        comment_id=str(comment_id),
        repository=full_name,
        number=number,
        author=author,
        body=str(comment.get("body") or ""),
        url=str(comment.get("html_url") or ""),
        event=event,
        is_pull_request=(
            event == "pull_request_review_comment"
            or isinstance(subject.get("pull_request"), dict)
        ),
        in_reply_to_id=str(in_reply_to) if isinstance(in_reply_to, (int, str)) else "",
    )


class GithubIngress:
    """Bounded background queue, deduplicated by comment identity."""

    def __init__(
        self,
        *,
        webhook_secret: Callable[[], str] = lambda: "",
        handle: Callable[[GithubComment], Awaitable[None]] | None = None,
        capacity: int = 256,
        verify_signature: Callable[[str, str, bytes], bool] = verify_signature,
    ) -> None:
        self._webhook_secret = webhook_secret
        self._handle = handle
        self._verify_signature = verify_signature
        self._queue: asyncio.Queue[GithubComment] = asyncio.Queue(maxsize=capacity)
        self._seen: OrderedDict[tuple[str, str], None] = OrderedDict()
        self._worker: asyncio.Task[None] | None = None

    async def webhook(self, request: Request) -> Response:
        """Authenticate, enqueue, and acknowledge without waiting for an agent."""
        body = await request.body()
        webhook_secret = self._webhook_secret()
        if not webhook_secret:
            log.warning(
                "a GitHub event was delivered but no webhook secret is configured, "
                "so it could not be verified and was ignored"
            )
            return JSONResponse(
                {"error": "GitHub webhook signing is not configured"}, status_code=503
            )
        if not self._verify_signature(
            webhook_secret, request.headers.get("x-hub-signature-256", ""), body
        ):
            return JSONResponse({"error": "invalid GitHub signature"}, status_code=401)
        event = request.headers.get("x-github-event", "")
        if event == "ping":
            # The one-off delivery GitHub sends when the webhook is saved.
            return JSONResponse({"ok": True})
        try:
            payload = json.loads(body)
        except ValueError:
            return JSONResponse({"error": "invalid GitHub event"}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse({"error": "invalid GitHub event"}, status_code=400)
        return Response(status_code=200 if self.accept(event, payload) else 503)

    def accept(self, event: str, payload: Mapping[str, object]) -> bool:
        """Whether the delivery is settled -- queued, or deliberately ignored.

        False only when the queue is full, which is the one case where GitHub
        retrying the delivery later is what this process wants.
        """
        comment = comment_from_payload(event, payload)
        if comment is None:
            return True
        identity = (comment.event, comment.comment_id)
        if identity in self._seen:
            return True
        if self._queue.full():
            return False
        self._queue.put_nowait(comment)
        self._seen[identity] = None
        while len(self._seen) > _SEEN_LIMIT:
            self._seen.popitem(last=False)
        if self._worker is None:
            self._worker = asyncio.create_task(self._run())
        return True

    async def _run(self) -> None:
        while True:
            comment = await self._queue.get()
            try:
                if self._handle is None:
                    log.info(
                        "no GitHub handler is wired; dropped comment %s on %s#%s",
                        comment.comment_id, comment.repository, comment.number,
                    )
                else:
                    await self._handle(comment)
            except Exception:
                log.exception("GitHub comment handling failed")
            finally:
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
            self._worker = None
