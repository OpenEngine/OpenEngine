"""What became of each GitHub comment, for the page that shows it.

Everything a comment does to this process happens out of sight. GitHub is
acknowledged in milliseconds, the work runs behind a queue, and the evidence
afterwards is a reply on the pull request and a line in this process's log --
enough to debug a delivery from a terminal, and not enough to answer "has
Engine seen my comment yet", which is the question somebody in front of the web
UI actually has.

So each comment keeps one row, rewritten in place as its turn moves through:
seen, picked up, forwarded to a work order, answered -- or ignored, with the
reason, because a comment Engine deliberately did not act on looks exactly like
one it lost. A row says what this process did and never what the model said:
the reply it carries is the concierge's own fixed announcement, and the
comment's text is kept only as a short excerpt, which is what makes a row
recognisable as somebody's comment without republishing it.

Process-local and bounded, like the deduplication window it sits beside. This
is the recent past rather than an audit trail, and a restart is allowed to
forget it -- by then GitHub's delivery log and the pull request itself are the
record, and neither of them is this process's to keep.
"""
from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - the ingress imports this module back
    from .github_ingress import GithubComment

#: How many comments are remembered. Smaller than the ingress's deduplication
#: window on purpose: that one exists so a redelivery is not acted on twice and
#: has to outlast every retry, while this is a panel somebody scrolls.
ACTIVITY_LIMIT = 50

#: How much of a comment is kept. Enough to recognise which comment a row is,
#: and short enough that the panel stays a list of events rather than becoming
#: a second, worse rendering of the conversation.
EXCERPT_LIMIT = 240

#: Where a comment is in the one path through this process. `queued` and
#: `working` are in flight; the rest are where a comment comes to rest.
QUEUED = "queued"
WORKING = "working"
DISPATCHED = "dispatched"
REPLIED = "replied"
IGNORED = "ignored"
FAILED = "failed"
#: The worker finished with a comment and nothing more specific was recorded.
#: The concierge always says what it did, so this is what a deployment that
#: wired its own handler sees.
HANDLED = "handled"


@dataclass(frozen=True, slots=True)
class CommentActivity:
    """One comment, and what this process has done about it so far.

    The timestamps are kept separately rather than collapsed into `status`
    because they are the answer to "how long did it sit in the queue" and
    "did the reply take a minute or three", which is most of why anybody opens
    this panel twice.
    """

    comment_id: str
    event: str
    repository: str
    number: int
    author: str
    url: str
    excerpt: str = ""
    status: str = QUEUED
    #: Why a comment was ignored, or how a turn failed. Empty otherwise.
    detail: str = ""
    #: The work order this comment's feedback reached, as the concierge
    #: reported it -- not the one that happens to own the pull request today.
    run_id: str = ""
    run_url: str = ""
    #: Whether that work order was started for this comment rather than
    #: already in flight. A comment that created the work it is asking about
    #: reads very differently from one that nudged work already running.
    started_run: bool = False
    #: The fixed announcement posted back to the pull request.
    reply: str = ""
    seen_at: float = 0.0
    started_at: float = 0.0
    dispatched_at: float = 0.0
    replied_at: float = 0.0


def _excerpt(body: str) -> str:
    text = " ".join(body.split())
    return text if len(text) <= EXCERPT_LIMIT else text[: EXCERPT_LIMIT - 1] + "…"


class GithubActivityLog:
    """A bounded, newest-last record of the comments this process has handled.

    The updates after `started` name no comment, because the callbacks that
    make them -- the concierge steering a work order, the concierge replying --
    are given a conversation and not a delivery. One slot holds the comment
    being worked on instead, which is sound for the same reason the concierge's
    own single-slot bookkeeping is: the ingress runs one worker, and the
    concierge serializes turns, so exactly one comment is ever in flight.
    """

    def __init__(self, *, limit: int = ACTIVITY_LIMIT, now: Callable[[], float] = time.time) -> None:
        self._entries: OrderedDict[tuple[str, str], CommentActivity] = OrderedDict()
        self._current: tuple[str, str] | None = None
        self._limit = limit
        self._now = now

    @staticmethod
    def _key(comment: GithubComment) -> tuple[str, str]:
        return (comment.event, comment.comment_id)

    def seen(self, comment: GithubComment) -> None:
        """A delivery got past the signature and into the queue.

        A comment can arrive here twice: a turn that failed is forgotten by the
        ingress so GitHub can redeliver it. The row starts over, keeping only
        the work order an earlier attempt forwarded to -- the concierge
        remembers that too, and answers the redelivery from it rather than
        forwarding again, so a row that dropped it would be the one thing on
        the page contradicting what the pull request was told.
        """
        key = self._key(comment)
        earlier = self._entries.get(key)
        self._entries[key] = CommentActivity(
            comment_id=comment.comment_id,
            event=comment.event,
            repository=comment.repository,
            number=comment.number,
            author=comment.author,
            url=comment.url,
            excerpt=_excerpt(comment.body),
            status=QUEUED,
            run_id="" if earlier is None else earlier.run_id,
            run_url="" if earlier is None else earlier.run_url,
            started_run=False if earlier is None else earlier.started_run,
            dispatched_at=0.0 if earlier is None else earlier.dispatched_at,
            seen_at=self._now(),
        )
        self._entries.move_to_end(key)
        while len(self._entries) > self._limit:
            forgotten, _ = self._entries.popitem(last=False)
            if forgotten == self._current:
                self._current = None

    def started(self, comment: GithubComment) -> None:
        """The worker picked this comment up; later updates belong to it."""
        key = self._key(comment)
        self._current = key
        if key not in self._entries:
            # Handed a comment nothing recorded arriving -- a handler driven
            # directly, in a test or by a future caller. Recording it here is
            # better than dropping every update that follows.
            self.seen(comment)
            self._current = key
        self._update(status=WORKING, started_at=self._now())

    def dispatched(
        self, run_id: str, run_url: str = "", *, started_run: bool = False
    ) -> None:
        self._update(status=DISPATCHED, run_id=run_id, run_url=run_url,
                     started_run=started_run, dispatched_at=self._now())

    def replied(self, text: str) -> None:
        self._update(status=REPLIED, reply=text, replied_at=self._now())

    def finished(self, comment: GithubComment) -> None:
        """The worker is done with this comment, however it went."""
        if self._current == self._key(comment):
            entry = self._entries.get(self._current)
            if entry is not None and entry.status == WORKING:
                self._update(status=HANDLED)
            self._current = None

    def ignored(self, reason: str) -> None:
        self._settle(status=IGNORED, detail=reason)

    def failed(self, reason: str) -> None:
        self._settle(status=FAILED, detail=reason)

    def _settle(self, **fields: object) -> None:
        self._update(**fields)
        self._current = None

    def _update(self, **fields: object) -> None:
        if self._current is None:
            return
        entry = self._entries.get(self._current)
        if entry is None:
            self._current = None
            return
        self._entries[self._current] = replace(entry, **fields)

    def recent(self) -> tuple[CommentActivity, ...]:
        """Everything remembered, newest first, which is how it is read."""
        return tuple(reversed(self._entries.values()))


def _comment_json(entry: CommentActivity, run_id: str) -> dict[str, object]:
    return {
        "commentId": entry.comment_id,
        "event": entry.event,
        "repository": entry.repository,
        "number": entry.number,
        "author": entry.author,
        "url": entry.url,
        "excerpt": entry.excerpt,
        "status": entry.status,
        "detail": entry.detail,
        # Which work order the comment belongs to: the one its feedback
        # reached, or -- for a comment ignored or still in flight -- whichever
        # one opened the pull request.
        "runId": entry.run_id or run_id,
        "dispatchedRunId": entry.run_id,
        "runUrl": entry.run_url,
        "startedRun": entry.started_run,
        "reply": entry.reply,
        "seenAt": entry.seen_at,
        "startedAt": entry.started_at,
        "dispatchedAt": entry.dispatched_at,
        "repliedAt": entry.replied_at,
    }


def activity_json(
    entries: Sequence[CommentActivity],
    *,
    owners: Mapping[tuple[str, int], str] | None = None,
    repository: str = "",
    configured: bool = False,
    queued: int = 0,
    working: bool = False,
    sessions: int = 0,
    run_id: str,
) -> dict[str, object]:
    """The wire shape one WorkOrder's comment panel reads.

    ``run_id`` is required and always narrows, because a comment is only ever
    read beside the work it steered. There is deliberately no way to ask this
    for every comment at once: the process remembers comments about work that
    is none of the asking WorkOrder's business, and that listing is not the
    API's to hand out.

    ``owners`` maps a pull request to the work order that opened it, resolved
    by the caller rather than remembered here: ownership is written down when a
    pull request is opened, which can happen after a comment on it was
    recorded, and a row is more use attributed late than not at all.
    """
    owned = owners or {}
    comments = (
        _comment_json(entry, owned.get((entry.repository.lower(), entry.number), ""))
        for entry in entries
    )
    return {
        "repository": repository,
        "configured": configured,
        "queued": queued,
        "working": working,
        "sessions": sessions,
        "comments": [c for c in comments if c["runId"] == run_id],
    }


__all__ = [
    "ACTIVITY_LIMIT",
    "DISPATCHED",
    "EXCERPT_LIMIT",
    "FAILED",
    "HANDLED",
    "IGNORED",
    "QUEUED",
    "REPLIED",
    "WORKING",
    "CommentActivity",
    "GithubActivityLog",
    "activity_json",
]
