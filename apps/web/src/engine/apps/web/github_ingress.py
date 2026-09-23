"""Verified GitHub deliveries enter here; the agent that answers runs behind a queue.

The GitHub counterpart of `slack_ingress.py`. GitHub gives a webhook ten
seconds and retries a delivery that took longer, so the route authenticates,
enqueues, and acknowledges rather than waiting for the work: a reply that
arrived late would otherwise be indistinguishable from a second copy of the
same comment.

Comments and issue assignments ask for work; merges accept the work, and
closing a pull request unmerged rejects it.
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
from typing import TYPE_CHECKING

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

if TYPE_CHECKING:  # pragma: no cover - imported for the type alone
    from .github_activity import GithubActivityLog

log = logging.getLogger(__name__)

#: The delivery kinds a comment arrives as.
COMMENT_EVENTS = frozenset({"issue_comment", "pull_request_review_comment"})

#: The delivery kind a merge arrives as. A pull request's whole lifecycle lands
#: on this event -- opened, labelled, synchronized, closed -- and only a close,
#: merged or not, is read here. Anything else is acknowledged and dropped, as is
#: any other event: a GitHub app subscribed to more than Engine reads is a
#: configuration this route tolerates rather than an error it reports.
MERGE_EVENT = "pull_request"

#: Initial affiliation filter. These labels do not prove write access; the
#: concierge checks effective repository permissions before steering a run.
TRUSTED_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})

#: The most a delivery may weigh. The route is reachable without a session, so
#: a body is buffered before anything about it is trusted: without a ceiling,
#: unsigned requests are a way to spend this process's memory. GitHub caps its
#: own payloads at 25 MB, and a comment event is orders of magnitude smaller
#: than this limit.
MAX_BODY_BYTES = 2 * 1024 * 1024

#: How many delivery identities are remembered for deduplication.
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


@dataclass(frozen=True)
class GithubMerge:
    """A pull request that has just been closed, as this process reads it.

    Merged is a person accepting the work; closed unmerged is one rejecting it.
    """

    repository: str
    number: int
    merged_by: str
    """Whoever merged or closed. A close with no person behind it is not read."""
    url: str
    merged: bool = True


@dataclass(frozen=True)
class GithubAssignment:
    """An issue assigned to the configured Engine account."""

    repository: str
    number: int
    assignee: str
    sender: str
    title: str
    body: str
    url: str


def assignment_from_payload(
    event: str, payload: Mapping[str, object], *, self_login: str = ""
) -> GithubAssignment | None:
    if event != "issues" or payload.get("action") != "assigned" or not self_login:
        return None
    issue, repository = payload.get("issue"), payload.get("repository")
    assignee, sender = payload.get("assignee"), payload.get("sender")
    if not all(isinstance(item, dict) for item in (issue, repository, assignee, sender)):
        return None
    login = assignee.get("login")
    if not isinstance(login, str) or login.lower() != self_login.lower():
        return None
    if "pull_request" in issue or issue.get("state") != "open":
        return None
    number, full_name, actor = issue.get("number"), repository.get("full_name"), sender.get("login")
    if not isinstance(number, int) or isinstance(number, bool) or number < 1:
        return None
    if not isinstance(full_name, str) or not full_name or not isinstance(actor, str) or not actor:
        return None
    return GithubAssignment(
        repository=full_name, number=number, assignee=login, sender=actor,
        title=str(issue.get("title") or ""), body=str(issue.get("body") or ""),
        url=str(issue.get("html_url") or ""),
    )


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
    if event not in COMMENT_EVENTS or payload.get("action") != "created":
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


def merge_from_payload(
    event: str, payload: Mapping[str, object], *, self_login: str = ""
) -> GithubMerge | None:
    """The merge or close in a delivery, or ``None`` for anything that is not one.

    A merge accepts the work and a close without merging rejects it, so
    ``merged`` is read alongside the action. Either is the point the work
    order's pull request is actually closed out; an approving review is not,
    since the branch can take more commits and another round of review after
    one.

    Who closed is checked as strictly as who commented, and for the reason the
    ``human_review`` gate exists. The gate asks for a person to look at the
    diff, and write access is not that property: a merge queue, an auto-merge
    firing when CI turns green, a Dependabot-style app, or Engine's own GitHub
    App all hold write access and none of them has read anything. So a merge
    whose actor is a bot is refused here rather than allowed to release a gate
    nobody read the diff for, and so is a bot's close -- a stale-branch sweeper
    has judged nothing either. Engine's own merge is refused here only when
    ``self_login`` is configured; the handler checks it again against the
    login Engine's credentials resolve to, which needs the forge.
    """
    if event != MERGE_EVENT or payload.get("action") != "closed":
        return None
    pull_request = payload.get("pull_request")
    repository = payload.get("repository")
    if not isinstance(pull_request, dict) or not isinstance(repository, dict):
        return None
    merged = pull_request.get("merged")
    if not isinstance(merged, bool):
        return None
    # GitHub names who merged on the pull request; who closed it unmerged is
    # only the delivery's sender.
    actor = pull_request.get("merged_by") if merged else payload.get("sender")
    if not isinstance(actor, dict) or actor.get("type") == "Bot":
        # No account at all is refused with the bots: a close Engine cannot
        # attribute to a person is not a person having reviewed the work.
        return None
    login = actor.get("login")
    if not isinstance(login, str) or not login:
        return None
    if self_login and login.lower() == self_login.lower():
        # A machine account holding a personal access token is an ordinary
        # `User` to GitHub, so the bot type above does not catch Engine's own.
        # GitHub logins are case-insensitive, so the comparison is too.
        return None
    full_name, number = repository.get("full_name"), pull_request.get("number")
    if not isinstance(full_name, str) or not full_name:
        return None
    if not isinstance(number, int) or isinstance(number, bool):
        return None
    return GithubMerge(
        repository=full_name,
        number=number,
        merged_by=login,
        url=str(pull_request.get("html_url") or ""),
        merged=merged,
    )


class GithubIngress:
    """Bounded background queue, deduplicated by delivery identity."""

    def __init__(
        self,
        *,
        webhook_secret: Callable[[], str] = lambda: "",
        repository: str = "",
        handle: Callable[[GithubComment], Awaitable[None]] | None = None,
        handle_merge: Callable[[GithubMerge], Awaitable[None]] | None = None,
        handle_assignment: Callable[[GithubAssignment], Awaitable[None]] | None = None,
        self_login: Callable[[], str] = lambda: "",
        capacity: int = 256,
        max_body_bytes: int = MAX_BODY_BYTES,
        verify_signature: Callable[[str, str, bytes], bool] = verify_signature,
        activity: GithubActivityLog | None = None,
    ) -> None:
        self._webhook_secret = webhook_secret
        self._repository = repository
        self._self_login = self_login
        self._handle = handle
        self._handle_merge = handle_merge
        self._handle_assignment = handle_assignment
        self._verify_signature = verify_signature
        self._max_body_bytes = max_body_bytes
        # Where a comment's progress is written down for the web UI. Recording
        # is bookkeeping and never a reason to refuse a delivery, so a missing
        # log is a deployment with no panel rather than a failure here.
        self._activity = activity
        self._queue: asyncio.Queue[tuple[tuple[str, str], GithubComment | GithubMerge | GithubAssignment]] = (
            asyncio.Queue(maxsize=capacity)
        )
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
        assignment = assignment_from_payload(event, payload, self_login=self._self_login())
        if assignment is not None:
            return self._enqueue(
                assignment, "assigned issue",
                ("issues", f"{assignment.repository.lower()}#{assignment.number}"),
                wired=self._handle_assignment is not None,
            )
        comment = comment_from_payload(event, payload, self_login=self._self_login())
        if comment is not None:
            return self._enqueue(
                comment, "comment", (comment.event, comment.comment_id),
                wired=self._handle is not None,
            )
        merged = merge_from_payload(event, payload, self_login=self._self_login())
        if merged is not None:
            # By the pull request and its verdict rather than by a delivery id:
            # what is acted on is that this pull request is merged, or closed,
            # and a second delivery saying so again asks for nothing new.
            verdict = "merged" if merged.merged else "closed"
            return self._enqueue(
                merged, f"{verdict} pull request",
                (MERGE_EVENT, f"{merged.repository.lower()}#{merged.number}/{verdict}"),
                wired=self._handle_merge is not None,
            )
        # Nothing to do with this delivery, whether or not a handler is
        # wired: settle it, so a webhook subscribed to more events than
        # Engine reads does not retry every one of them forever.
        return True

    def _enqueue(
        self,
        delivery: GithubComment | GithubMerge | GithubAssignment,
        subject: str,
        identity: tuple[str, str],
        *,
        wired: bool,
    ) -> bool:
        """Queue one read delivery, or say why it is being refused."""
        if not self._repository:
            log.warning(
                "a GitHub %s was delivered but no target repository is configured", subject
            )
            return False
        if delivery.repository.lower() != self._repository.lower():
            # A shared App secret authenticates deliveries from other repos too.
            # Ignore them before queueing or remembering their identities.
            return True
        if not wired:
            log.warning(
                "a GitHub %s was delivered but nothing is wired to answer it; "
                "refusing the delivery rather than acknowledging and dropping it", subject,
            )
            return False
        if identity in self._seen:
            return True
        if self._queue.full():
            return False
        self._queue.put_nowait((identity, delivery))
        if self._activity is not None and isinstance(delivery, GithubComment):
            self._activity.seen(delivery)
        self._seen[identity] = None
        while len(self._seen) > _SEEN_LIMIT:
            self._seen.popitem(last=False)
        if self._worker is None:
            self._worker = asyncio.create_task(self._run())
        return True

    async def _run(self) -> None:
        while True:
            identity, delivery = await self._queue.get()
            # Only a comment has a row on the panel; a merge is a verdict this
            # process acts on, not a conversation somebody is following.
            comment = delivery if isinstance(delivery, GithubComment) else None
            try:
                if isinstance(delivery, GithubComment):
                    assert self._handle is not None  # nothing is queued without one
                    if self._activity is not None:
                        self._activity.started(delivery)
                    await self._handle(delivery)
                elif isinstance(delivery, GithubAssignment):
                    assert self._handle_assignment is not None
                    await self._handle_assignment(delivery)
                else:
                    assert self._handle_merge is not None
                    await self._handle_merge(delivery)
            except Exception as failure:
                if comment is not None and self._activity is not None:
                    self._activity.failed(str(failure) or type(failure).__name__)
                # The delivery was acknowledged, so GitHub will not retry on its
                # own; forgetting it is what makes a redelivery -- by hand from
                # the delivery log, or by a later duplicate -- able to pick the
                # work back up instead of being deduplicated away.
                self._seen.pop(identity, None)
                log.exception(
                    "GitHub delivery handling failed; %s on %s#%s can be redelivered",
                    identity[1], delivery.repository, delivery.number,
                )
            finally:
                if comment is not None and self._activity is not None:
                    self._activity.finished(comment)
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
