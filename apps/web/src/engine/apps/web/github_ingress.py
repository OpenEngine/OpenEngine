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

#: Initial affiliation filter. These labels do not prove write access; the
#: concierge checks effective repository permissions before steering a run.
TRUSTED_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})

#: The most a delivery may weigh. The route is reachable without a session, so
#: a body is buffered before anything about it is trusted: without a ceiling,
#: unsigned requests are a way to spend this process's memory. GitHub caps its
#: own payloads at 25 MB, and a comment event is orders of magnitude smaller
#: than this limit.
MAX_BODY_BYTES = 2 * 1024 * 1024

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


def comment_from_payload(
    event: str, payload: Mapping[str, object], *, self_login: str = ""
) -> GithubComment | None:
    """The comment in a delivery, or ``None`` for anything not worth an agent.

    Edits and deletions are excluded with everything else: only a new comment
    is somebody asking for something, and only from an affiliated author.

    ``self_login`` is the GitHub account Engine posts as, whose own comments are
    never answered. A GitHub app is recognisable by its ``Bot`` user type, but a
    personal access token belonging to a machine user is not: such an account is
    an ordinary ``User`` and typically a collaborator, so it would pass every
    other check here and Engine would answer itself forever.
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
    login = user.get("login")
    if self_login and isinstance(login, str) and login.lower() == self_login.lower():
        # GitHub logins are case-insensitive, so the comparison is too.
        return None
    association = comment.get("author_association")
    if association not in TRUSTED_ASSOCIATIONS:
        log.info(
            "ignored a GitHub comment from %s, whose association with the repository is %s",
            user.get("login"), association,
        )
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
        repository: str = "",
        handle: Callable[[GithubComment], Awaitable[None]] | None = None,
        self_login: Callable[[], str] = lambda: "",
        capacity: int = 256,
        max_body_bytes: int = MAX_BODY_BYTES,
        verify_signature: Callable[[str, str, bytes], bool] = verify_signature,
    ) -> None:
        self._webhook_secret = webhook_secret
        self._repository = repository
        self._self_login = self_login
        self._handle = handle
        self._verify_signature = verify_signature
        self._max_body_bytes = max_body_bytes
        self._queue: asyncio.Queue[GithubComment] = asyncio.Queue(maxsize=capacity)
        self._seen: OrderedDict[tuple[str, str], None] = OrderedDict()
        self._worker: asyncio.Task[None] | None = None

    async def _read_body(self, request: Request) -> bytes | None:
        """The delivery's body, or ``None`` if it outgrows what one may weigh.

        ``Request.body()`` buffers whatever arrives, so the ceiling is enforced
        here rather than after: a declared length is refused before a byte is
        read, and a chunked body is abandoned as soon as it passes the limit.
        """
        declared = request.headers.get("content-length")
        if declared is not None:
            try:
                if int(declared) > self._max_body_bytes:
                    return None
            except ValueError:
                return None
        chunks: list[bytes] = []
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > self._max_body_bytes:
                return None
            chunks.append(chunk)
        return b"".join(chunks)

    async def webhook(self, request: Request) -> Response:
        """Authenticate, enqueue, and acknowledge without waiting for an agent."""
        body = await self._read_body(request)
        if body is None:
            log.warning("refused a GitHub delivery larger than %s bytes", self._max_body_bytes)
            return JSONResponse({"error": "GitHub event is too large"}, status_code=413)
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
            # The one-off delivery GitHub sends when the webhook is saved. It
            # is answered even with nothing wired behind the route, so that the
            # webhook can be configured before whatever answers it exists.
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

        False when there is nothing to queue into or nowhere to queue it: a
        full queue, or no handler wired. Those are the cases where a failed
        delivery GitHub can redeliver beats a 200 that loses the comment.
        """
        comment = comment_from_payload(event, payload, self_login=self._self_login())
        if comment is None:
            # Nothing to do with this delivery, whether or not a handler is
            # wired: settle it, so a webhook subscribed to more events than
            # Engine reads does not retry every one of them forever.
            return True
        if not self._repository:
            log.warning("a GitHub comment was delivered but no target repository is configured")
            return False
        if comment.repository.lower() != self._repository.lower():
            # A shared App secret authenticates deliveries from other repos too.
            # Ignore them before queueing or remembering their comment identities.
            return True
        if self._handle is None:
            log.warning(
                "a GitHub comment was delivered but nothing is wired to answer it; "
                "refusing the delivery rather than acknowledging and dropping it"
            )
            return False
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
                assert self._handle is not None  # nothing is queued without one
                await self._handle(comment)
            except Exception:
                # The delivery was acknowledged, so GitHub will not retry on its
                # own; forgetting the comment is what makes a redelivery -- by
                # hand from the delivery log, or by a later duplicate -- able to
                # pick the work back up instead of being deduplicated away.
                self._seen.pop((comment.event, comment.comment_id), None)
                log.exception(
                    "GitHub comment handling failed; %s on %s#%s can be redelivered",
                    comment.comment_id, comment.repository, comment.number,
                )
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
