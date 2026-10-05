"""Verified GitHub deliveries enter here; the agent that answers runs behind a queue.

The GitHub counterpart of `slack_ingress.py`. GitHub gives a webhook ten
seconds and retries a delivery that took longer, so the route authenticates,
enqueues, and acknowledges rather than waiting for the work: a reply that
arrived late would otherwise be indistinguishable from a second copy of the
same comment.

Comments, issue assignments, and review requests ask for work; merges accept
the work.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
import time
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
#: on this event -- opened, labelled, synchronized, closed -- and only a close
#: that merged is read here. Anything else is acknowledged and dropped, as is
#: any other event: a GitHub app subscribed to more than Engine reads is a
#: configuration this route tolerates rather than an error it reports.
MERGE_EVENT = "pull_request"

#: Initial affiliation filter. These labels do not prove write access; the
#: concierge checks effective repository permissions before steering a run.
TRUSTED_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})

# Avoid reading email addresses as mentions; include GitHub App bot logins.
_MENTION = re.compile(r"(?<![\w@])@([A-Za-z0-9]+(?:-[A-Za-z0-9]+)*(?:\[bot\])?)(?![\w-])")

#: The most a delivery may weigh. The route is reachable without a session, so
#: a body is buffered before anything about it is trusted: without a ceiling,
#: unsigned requests are a way to spend this process's memory. GitHub caps its
#: own payloads at 25 MB, and a comment event is orders of magnitude smaller
#: than this limit.
MAX_BODY_BYTES = 2 * 1024 * 1024

#: How many delivery identities are remembered for deduplication.
_SEEN_LIMIT = 4096

#: How long one delivery may hold the worker before the log says so, and how
#: often it says so again. One worker serves every delivery, so a handler that
#: never returns silences every comment after it while the route keeps
#: answering GitHub 200 -- from outside, indistinguishable from comments that
#: were read and deliberately ignored. A concierge turn is bounded at three
#: minutes, so a minute is long for anything but a model turn.
STALL_WARNING_SECONDS = 60.0


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
    #: GitHub's numeric id for ``author``, which outlives a renamed login.
    author_id: int = 0


@dataclass(frozen=True)
class GithubMerge:
    """A pull request that has just been merged, as this process reads it."""

    repository: str
    number: int
    merged_by: str
    """Whoever merged. A merge with no person behind it is not read at all."""
    url: str


@dataclass(frozen=True)
class GithubAssignment:
    """An issue assigned to the authenticated Engine account."""

    repository: str
    number: int
    assignee: str
    sender: str
    title: str
    body: str
    url: str
    sender_id: int = 0


@dataclass(frozen=True)
class GithubReviewRequest:
    """A pull request whose review was requested from the Engine account."""

    repository: str
    number: int
    sender: str
    title: str
    url: str
    branch: str
    """The pull request's head branch, on the repository itself."""
    head_sha: str
    sender_id: int = 0


#: The action a review request arrives as, on the pull request event.
REVIEW_REQUESTED = "review_requested"


def review_request_from_payload(
    event: str, payload: Mapping[str, object], *, self_login: str = ""
) -> GithubReviewRequest | None:
    """The review asked of Engine in a delivery, or ``None`` for anything else.

    Only a request naming Engine's own account, on an open pull request whose
    head branch lives on the repository itself: a fork's code would run the
    review's agents, as `engine review` refuses too.
    """
    if event != MERGE_EVENT or payload.get("action") != REVIEW_REQUESTED or not self_login:
        return None
    pull_request, repository = payload.get("pull_request"), payload.get("repository")
    reviewer, sender = payload.get("requested_reviewer"), payload.get("sender")
    if not all(isinstance(item, dict) for item in (pull_request, repository, reviewer, sender)):
        return None
    login = reviewer.get("login")
    if not isinstance(login, str) or login.lower() != self_login.lower():
        return None
    if pull_request.get("state") != "open" or sender.get("type") == "Bot":
        return None
    head = pull_request.get("head")
    head_repository = head.get("repo") if isinstance(head, dict) else None
    full_name, actor = repository.get("full_name"), sender.get("login")
    number = pull_request.get("number")
    if not isinstance(number, int) or isinstance(number, bool) or number < 1:
        return None
    if not isinstance(full_name, str) or not full_name or not isinstance(actor, str) or not actor:
        return None
    if not isinstance(head_repository, dict) or not isinstance(head_repository.get("full_name"), str):
        return None
    if head_repository["full_name"].lower() != full_name.lower():
        log.info("ignored a review request on %s#%s, whose branch is on a fork", full_name, number)
        return None
    branch, sha = head.get("ref"), head.get("sha")
    if not isinstance(branch, str) or not branch or not isinstance(sha, str) or not sha:
        return None
    return GithubReviewRequest(
        repository=full_name, number=number, sender=actor,
        title=str(pull_request.get("title") or ""),
        url=str(pull_request.get("html_url") or ""),
        branch=branch, head_sha=sha, sender_id=_account_id(sender),
    )


def describe(delivery: GithubComment | GithubMerge | GithubAssignment | GithubReviewRequest) -> str:
    """One delivery as a log line names it: what, who, and where."""
    if isinstance(delivery, GithubComment):
        return (f"comment {delivery.comment_id} by {delivery.author} "
                f"on {delivery.repository}#{delivery.number}")
    if isinstance(delivery, GithubAssignment):
        return f"assignment of {delivery.repository}#{delivery.number} by {delivery.sender}"
    if isinstance(delivery, GithubReviewRequest):
        return f"review request on {delivery.repository}#{delivery.number} by {delivery.sender}"
    return f"merge of {delivery.repository}#{delivery.number} by {delivery.merged_by}"


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
        sender_id=_account_id(sender),
    )


def github_requester(account_id: int, login: str) -> str | None:
    """The provider-qualified identity a work order records as its requester."""
    return f"github:{account_id}:{login}" if account_id > 0 and login else None


def github_co_author(requester: str | None) -> str:
    """A GitHub requester as `login <noreply email>`, or empty for anyone else.

    The noreply address links a commit to the account without its real email.
    """
    provider, _, rest = (requester or "").partition(":")
    account_id, _, login = rest.partition(":")
    if provider != "github" or not account_id.isdigit() or not login:
        return ""
    return f"{login} <{account_id}+{login}@users.noreply.github.com>"


def _account_id(user: Mapping[str, object]) -> int:
    value = user.get("id")
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


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

    Comments mentioning other accounts are ignored unless they also mention
    ``self_login``. Without a configured login, this filter is not applied.
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
    body = str(comment.get("body") or "")
    if self_login:
        mentions = {match.lower() for match in _MENTION.findall(body)}
        if mentions and self_login.lower() not in mentions:
            return None
    in_reply_to = comment.get("in_reply_to_id")
    return GithubComment(
        comment_id=str(comment_id),
        repository=full_name,
        number=number,
        author=author,
        body=body,
        url=str(comment.get("html_url") or ""),
        event=event,
        is_pull_request=(
            event == "pull_request_review_comment"
            or isinstance(subject.get("pull_request"), dict)
        ),
        in_reply_to_id=str(in_reply_to) if isinstance(in_reply_to, (int, str)) else "",
        author_id=_account_id(user),
    )


def merge_from_payload(
    event: str, payload: Mapping[str, object], *, self_login: str = ""
) -> GithubMerge | None:
    """The merge in a delivery, or ``None`` for anything that is not one.

    A pull request closed without merging decides nothing -- the work was
    abandoned, not accepted -- so ``merged`` is what is read rather than the
    action alone. A merge is the point the work order's pull request is
    actually closed out; an approving review is not, since the branch can take
    more commits and another round of review after one.

    Who merged is checked as strictly as who commented, and for the reason the
    ``human_review`` gate exists. The gate asks for a person to look at the
    diff, and write access is not that property: a merge queue, an auto-merge
    firing when CI turns green, a Dependabot-style app, or Engine's own GitHub
    App all hold write access and none of them has read anything. So a merge
    whose actor is a bot is refused here rather than allowed to release a gate
    nobody read the diff for. Engine's own merge is refused here only when
    ``self_login`` is supplied; the handler checks it again against the
    login Engine's credentials resolve to, which needs the forge.
    """
    if event != MERGE_EVENT or payload.get("action") != "closed":
        return None
    pull_request = payload.get("pull_request")
    repository = payload.get("repository")
    if not isinstance(pull_request, dict) or not isinstance(repository, dict):
        return None
    if pull_request.get("merged") is not True:
        return None
    merged_by = pull_request.get("merged_by")
    if not isinstance(merged_by, dict) or merged_by.get("type") == "Bot":
        # No account at all is refused with the bots: a merge Engine cannot
        # attribute to a person is not a person having reviewed the work.
        return None
    login = merged_by.get("login")
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
    )


_Delivery = GithubComment | GithubMerge | GithubAssignment | GithubReviewRequest


def _asks_for_self_login(event: str, payload: Mapping[str, object]) -> bool:
    """Whether a delivery can only be read knowing the account Engine acts as."""
    return (event, payload.get("action")) in (("issues", "assigned"), (MERGE_EVENT, REVIEW_REQUESTED))


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
        handle_review_request: Callable[[GithubReviewRequest], Awaitable[None]] | None = None,
        authenticated_login: Callable[[str], Awaitable[str]] | None = None,
        may_act: Callable[
            [GithubComment | GithubAssignment | GithubReviewRequest], Awaitable[bool]
        ] | None = None,
        capacity: int = 256,
        max_body_bytes: int = MAX_BODY_BYTES,
        verify_signature: Callable[[str, str, bytes], bool] = verify_signature,
        activity: GithubActivityLog | None = None,
        stall_warning_seconds: float = STALL_WARNING_SECONDS,
    ) -> None:
        self._webhook_secret = webhook_secret
        self._repository = repository
        self._authenticated_login = authenticated_login
        # Whether whoever sent a comment or assignment can write to its
        # repository. Asked before any handler sees it, so nothing -- a work
        # order started, a comment forwarded to one -- happens for somebody
        # who could not have made that change themselves.
        self._may_act = may_act
        self._handle = handle
        self._handle_merge = handle_merge
        self._handle_assignment = handle_assignment
        self._handle_review_request = handle_review_request
        self._verify_signature = verify_signature
        self._max_body_bytes = max_body_bytes
        # Where a comment's progress is written down for the web UI. Recording
        # is bookkeeping and never a reason to refuse a delivery, so a missing
        # log is a deployment with no panel rather than a failure here.
        self._activity = activity
        self._queue: asyncio.Queue[tuple[tuple[str, str], _Delivery]] = asyncio.Queue(maxsize=capacity)
        self._seen: OrderedDict[tuple[str, str], None] = OrderedDict()
        self._worker: asyncio.Task[None] | None = None
        self._stall_warning_seconds = stall_warning_seconds

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
        self_login = ""
        if (
            (
                _asks_for_self_login(event, payload)
                or (event in COMMENT_EVENTS and payload.get("action") == "created")
            )
            and self._authenticated_login is not None
            and self._repository
        ):
            try:
                # Leave time to acknowledge within GitHub's ten-second deadline.
                async with asyncio.timeout(5):
                    self_login = await self._authenticated_login(self._repository)
            except Exception:
                log.warning(
                    "a GitHub %s delivery needed the self-login, but its lookup failed; "
                    "refusing the delivery", payload.get("action"),
                    exc_info=True,
                )
                return Response(status_code=503)
        return Response(
            status_code=200 if self.accept(
                event, payload, self_login=self_login,
                delivery_id=request.headers.get("x-github-delivery", ""),
            ) else 503
        )

    def accept(
        self, event: str, payload: Mapping[str, object], *, self_login: str = "",
        delivery_id: str = "",
    ) -> bool:
        """Whether the delivery is settled -- queued, or deliberately ignored.

        False when there is nothing to queue into or nowhere to queue it: a
        full queue, no handler wired, or an unresolved assignment login. A failed
        delivery GitHub can redeliver beats a 200 that loses the work.
        """
        if _asks_for_self_login(event, payload) and not self_login:
            log.warning(
                "a GitHub %s delivery needed the self-login, but none could be resolved; "
                "refusing the delivery rather than acknowledging and dropping it",
                payload.get("action"),
            )
            return False
        assignment = assignment_from_payload(event, payload, self_login=self_login)
        if assignment is not None:
            return self._enqueue(
                assignment, "assigned issue",
                ("issues", f"{assignment.repository.lower()}#{assignment.number}"),
                wired=self._handle_assignment is not None, delivery_id=delivery_id,
            )
        requested = review_request_from_payload(event, payload, self_login=self_login)
        if requested is not None:
            # By the commit under review: asking again after a push is asking
            # for a review of new work, asking twice for one commit is not.
            return self._enqueue(
                requested, "review request",
                (REVIEW_REQUESTED, f"{requested.repository.lower()}#{requested.number}@{requested.head_sha}"),
                wired=self._handle_review_request is not None, delivery_id=delivery_id,
            )
        comment = comment_from_payload(event, payload, self_login=self_login)
        if comment is not None:
            return self._enqueue(
                comment, "comment", (comment.event, comment.comment_id),
                wired=self._handle is not None, delivery_id=delivery_id,
            )
        merged = merge_from_payload(event, payload, self_login=self_login)
        if merged is not None:
            # By the pull request rather than by a delivery id: what is acted on
            # is that this pull request is merged, which happens once, and a
            # second delivery saying so again asks for nothing new.
            return self._enqueue(
                merged, "merged pull request",
                (MERGE_EVENT, f"{merged.repository.lower()}#{merged.number}"),
                wired=self._handle_merge is not None, delivery_id=delivery_id,
            )
        if event in COMMENT_EVENTS:
            # A comment is what somebody expects an answer to, so one that is
            # not read says so: an edit, a bot, Engine's own reply, or an
            # author without a trusted association.
            comment = payload.get("comment")
            user = comment.get("user") if isinstance(comment, dict) else None
            log.info(
                "ignored GitHub %s delivery %s (action %s, by %s): not a comment "
                "Engine acts on", event, delivery_id or "-", payload.get("action"),
                user.get("login") if isinstance(user, dict) else "unknown",
            )
        # Nothing to do with this delivery, whether or not a handler is
        # wired: settle it, so a webhook subscribed to more events than
        # Engine reads does not retry every one of them forever.
        return True

    def _enqueue(
        self,
        delivery: _Delivery,
        subject: str,
        identity: tuple[str, str],
        *,
        wired: bool,
        delivery_id: str = "",
    ) -> bool:
        """Queue one read delivery, or say why it is being refused."""
        named = f"{describe(delivery)} (delivery {delivery_id or '-'})"
        if not self._repository:
            log.warning(
                "a GitHub %s was delivered but no target repository is configured", subject
            )
            return False
        if delivery.repository.lower() != self._repository.lower():
            # A shared App secret authenticates deliveries from other repos too.
            # Ignore them before queueing or remembering their identities.
            log.info("ignored %s: this deployment answers %s", named, self._repository)
            return True
        if not wired:
            log.warning(
                "a GitHub %s was delivered but nothing is wired to answer it; "
                "refusing the delivery rather than acknowledging and dropping it", subject,
            )
            return False
        if identity in self._seen:
            log.info("ignored %s: already queued or handled", named)
            return True
        if self._queue.full():
            log.warning(
                "refused %s: the GitHub queue is full (%d waiting), so GitHub "
                "will report it failed and it can be redelivered",
                named, self._queue.qsize(),
            )
            return False
        log.info("queued %s; %d ahead of it", named, self._queue.qsize())
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
            named = describe(delivery)
            began = time.monotonic()
            log.info("handling %s", named)
            watchdog = asyncio.create_task(self._warn_while_stalled(named, began))
            try:
                if isinstance(delivery, GithubComment):
                    assert self._handle is not None  # nothing is queued without one
                    if self._activity is not None:
                        self._activity.started(delivery)
                    if self._may_act is None or await self._may_act(delivery):
                        await self._handle(delivery)
                elif isinstance(delivery, GithubAssignment):
                    assert self._handle_assignment is not None
                    if self._may_act is None or await self._may_act(delivery):
                        await self._handle_assignment(delivery)
                elif isinstance(delivery, GithubReviewRequest):
                    assert self._handle_review_request is not None
                    if self._may_act is None or await self._may_act(delivery):
                        await self._handle_review_request(delivery)
                else:
                    assert self._handle_merge is not None
                    await self._handle_merge(delivery)
                log.info(
                    "handled %s in %.1fs%s", named, time.monotonic() - began,
                    self._outcome(comment),
                )
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
                watchdog.cancel()
                if comment is not None and self._activity is not None:
                    self._activity.finished(comment)
                self._queue.task_done()

    async def _warn_while_stalled(self, named: str, began: float) -> None:
        """Say, while it lasts, that one delivery is holding up the rest."""
        while True:
            await asyncio.sleep(self._stall_warning_seconds)
            log.warning(
                "still handling %s after %.0fs; %d GitHub deliveries are waiting "
                "behind it", named, time.monotonic() - began, self._queue.qsize(),
            )

    def _outcome(self, comment: GithubComment | None) -> str:
        """What the activity log recorded for a comment, as a log suffix."""
        entry = (
            None if comment is None or self._activity is None
            else self._activity.entry(comment)
        )
        if entry is None:
            return ""
        parts = [entry.status]
        if entry.detail:
            parts.append(entry.detail)
        if entry.run_id:
            parts.append(f"work order {entry.run_id}")
        return ": " + ", ".join(parts)

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
