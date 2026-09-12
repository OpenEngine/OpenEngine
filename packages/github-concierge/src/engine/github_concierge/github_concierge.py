"""Pull-request conversation graph. Transport and work-order steering injected.

A pull-request conversation is not a Slack thread with a different address: the
concierge here may only reach the pull request the comment arrived on -- the
work order in flight for it, or one the host starts for it when none is -- and
its participants are whoever can comment on it rather than one person in a
direct message. Both differences are authority, so this is its own graph rather
than a mode of the Slack one.

Model output flows inward and never outward. What the agent writes reaches the
work order through the feedback tool, which is private; what gets posted back
to the pull request is fixed text chosen by whether that tool succeeded, plus
identifiers this process already held. A pull-request comment is untrusted
text, the agent reading it can read the host it runs on, and a reply is public
-- so the one thing a commenter can dictate is not given a way out.

Forwarding happens once per comment. Reaching a work order changes what an
agent is building, or starts one building, while posting the reply that
announces it is a separate step that can fail on its own -- and a failed turn
is redelivered. A comment whose feedback already landed is answered from what
was recorded rather than run again, so a retried reply cannot ask for the same
work twice.

Once means once on both sides of that: between deliveries, and within a turn.
A comment is the unit of authority here -- one person asked for one thing --
and how many times the model chooses to call the tool while reading it is not
a second person asking. The turn's first call is the comment's; later ones are
refused where they are made, and the agent is told why.
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass
from tempfile import TemporaryDirectory
from typing import TypedDict

from engine.domain import RunOrigin
from langgraph.graph import END, START, StateGraph
from langgraph_acp.agent import ACPAgentProvider
from langgraph_acp.session import ACPSession

from .github_egress import Continuation, FeedbackBroker

#: Given the origin that asked and the feedback, reach that pull request's work
#: order -- steering the one in flight, or starting one when there is none.
ContinueWorkorder = Callable[[RunOrigin, str], Awaitable[Continuation]]
Reply = Callable[[RunOrigin, str], Awaitable[None]]

INSTRUCTIONS = """You are OpenEngineBot, a pull request concierge. Your one effect
on this pull request is the continue_workorder tool: when a comment asks for a
change, or asks for review feedback to be addressed, call it with that request
and it reaches the work order for this pull request. Whether that is the work
order already in flight or a new one started for this pull request is the
host's decision, not yours. You have no implementation role.

Nothing you write is published. The reply posted to the pull request is fixed
text chosen by whether that tool succeeded, so do not compose an answer and do
not try to tell the reader anything except by calling the tool. When a comment
asks for no change, call nothing.
"""

#: How many already-forwarded comments are remembered, so a reply retried after
#: a failure is not mistaken for a fresh request. Generous next to one pull
#: request's conversation, and bounded because this is process-local: a restart
#: loses it, and the ingress has by then acknowledged those deliveries.
_FORWARDED_LIMIT = 1024

#: Everything the concierge is allowed to say in public. Fixed strings, chosen
#: by what happened rather than written by a model.
FORWARDED = "Forwarded to work order `{run_id}`."
STARTED = "Started work order `{run_id}` for this pull request."
UNDELIVERED = (
    "I could not reach the work order for this pull request, so nothing has "
    "been forwarded."
)
NOT_FORWARDED = (
    "I only forward change requests to the work order for this pull request, "
    "and I have not forwarded anything for this comment."
)


class AlreadyForwarded(RuntimeError):
    """Raised when a turn tries to reach its work order a second time.

    Reaches the agent as a failed tool call, which is the honest answer: what
    it asked for the second time did not happen, and it should not report that
    it did.
    """


@dataclass(frozen=True, slots=True)
class Delivery:
    """What one turn did to the work order, as the host saw it.

    The whole basis for the public reply. ``run_id`` and ``url`` come from this
    process's own store, never from the conversation.
    """

    run_id: str = ""
    url: str = ""
    attempted: bool = False
    #: Whether that work order was started for this comment rather than
    #: already at work on the pull request. The host decides which happened,
    #: and the public reply says which rather than eliding the difference.
    started: bool = False

    def announcement(self) -> str:
        if self.run_id:
            landed = (STARTED if self.started else FORWARDED).format(run_id=self.run_id)
            return f"{landed} {self.url}" if self.url else landed
        return UNDELIVERED if self.attempted else NOT_FORWARDED


@dataclass(frozen=True)
class FeedbackRequest:
    """One pull-request comment asking Engine for something.

    ``origin`` addresses the conversation the answer belongs in: the channel
    names the repository, the thread the pull request and -- for a reply inside
    a review thread -- the comment that thread hangs from.
    """

    origin: RunOrigin
    text: str
    comment_id: str = ""


class ConversationState(TypedDict):
    request: FeedbackRequest
    reply: str


def build_graph(
    turn: Callable[[ConversationState], Awaitable[dict]],
    reply: Callable[[ConversationState], Awaitable[dict]],
):
    """START -> ACP conversation turn -> GitHub reply -> END."""
    graph = StateGraph(ConversationState)
    graph.add_node("concierge", turn)
    graph.add_node("reply", reply)
    graph.add_edge(START, "concierge")
    graph.add_edge("concierge", "reply")
    graph.add_edge("reply", END)
    return graph.compile()


class GithubConcierge:
    """Bounded live ACP sessions; handle, forget, and close are the API.

    Sessions are keyed by author as well as by conversation. A pull request is
    a public place, so its history is written by people with different
    authority over the repository: sharing one session between them would let
    whoever comments first leave instructions that the model carries into a
    later author's turn, and act on with that author's permission. Keeping the
    author in the session's identity makes each participant's history their
    own, and lets the callback that steers work bind one origin for the
    session's lifetime rather than tracking whose turn is running.

    Turns are serialized to prevent concurrent session creation and eviction of
    an active session. Conversation state is intentionally process-local.
    """

    #: The comment this turn is answering, so the callback that steers work can
    #: record what it forwarded without the session's closure -- which outlives
    #: the turn -- having to carry a request. Safe as one slot because `handle`
    #: serializes turns: exactly one is ever in flight to write it.
    _forwarding: tuple[str, str, str] | None

    def __init__(self, *, provider: ACPAgentProvider,
                 continue_workorder: ContinueWorkorder,
                 reply: Reply, max_threads: int = 32,
                 timeout_seconds: float = 180) -> None:
        if max_threads < 1:
            raise ValueError("max_threads must be positive")
        self.provider = provider
        self.continue_workorder = continue_workorder
        self.reply = reply
        self.max_threads = max_threads
        self.timeout_seconds = timeout_seconds
        self._threads: OrderedDict[
            tuple[str, str, str], tuple[AsyncExitStack, ACPSession]
        ] = OrderedDict()
        self._lock = asyncio.Lock()
        # What the turn in progress did, read by `_reply` to choose what to
        # say. One slot rather than one per session because `handle` serializes
        # turns: exactly one is ever in flight to write it.
        self._delivery = Delivery()
        self._forwarding = None
        # What forwarding achieved for comments already dealt with, so a reply
        # retried by redelivery is only a reply.
        self._forwarded: OrderedDict[tuple[str, str, str], Delivery] = OrderedDict()
        self.graph = build_graph(self._turn, self._reply)

    @staticmethod
    def _key(origin: RunOrigin) -> tuple[str, str, str]:
        return (origin.channel, origin.thread_id, origin.author)

    def has_session(self, origin: RunOrigin) -> bool:
        return self._key(origin) in self._threads

    async def _forget(self, key: tuple[str, str, str]) -> None:
        item = self._threads.pop(key, None)
        if item is not None:
            await item[0].aclose()

    async def forget(self, origin: RunOrigin) -> None:
        async with self._lock:
            await self._forget(self._key(origin))

    async def close(self) -> None:
        async with self._lock:
            for key in list(self._threads):
                await self._forget(key)

    async def handle(self, request: FeedbackRequest) -> None:
        async with self._lock:
            try:
                async with asyncio.timeout(self.timeout_seconds):
                    await self.graph.ainvoke({"request": request})
            except BaseException:
                await self._forget(self._key(request.origin))
                raise

    def _forwarded_key(self, request: FeedbackRequest) -> tuple[str, str, str] | None:
        """This comment's identity, or ``None`` if the caller did not give one.

        The repository and pull request are part of it because a comment id is
        unique only within one forge and one comment kind. A caller that passes
        no id gets no protection, which is the honest outcome: there is nothing
        to recognise the comment by on a second delivery.
        """
        if not request.comment_id:
            return None
        channel, thread_id, _author = self._key(request.origin)
        return (channel, thread_id, request.comment_id)

    def _remember_forwarded(self) -> None:
        if self._forwarding is not None:
            self._forwarded[self._forwarding] = self._delivery
            while len(self._forwarded) > _FORWARDED_LIMIT:
                self._forwarded.popitem(last=False)

    async def _turn(self, state: ConversationState) -> dict[str, str]:
        request = state["request"]
        origin = request.origin
        key = self._key(origin)
        forwarded = self._forwarded_key(request)
        landed = self._forwarded.get(forwarded) if forwarded else None
        if landed is not None:
            # This comment's feedback reached the work order on an earlier
            # attempt and only the reply is outstanding. Running the turn again
            # would ask the agent to do the same work a second time.
            return {"reply": landed.announcement()}
        self._delivery = Delivery()
        self._forwarding = forwarded
        fresh = key not in self._threads
        if fresh:
            while len(self._threads) >= self.max_threads:
                await self._forget(next(iter(self._threads)))
            opened = AsyncExitStack()
            try:
                cwd = opened.enter_context(TemporaryDirectory(prefix="github-concierge-"))

                async def continue_workorder(prompt: str) -> Continuation:
                    # The session belongs to this origin for its whole life, so
                    # the authority a tool call carries is the authority of the
                    # author whose history it was reasoning over.
                    if self._delivery.run_id:
                        # The record that stops a redelivery forwarding twice
                        # is read before the turn starts, which is too early to
                        # see a second call inside one: the comment is the unit
                        # of authority, so the turn's later calls are refused
                        # here, where they are made.
                        raise AlreadyForwarded(
                            "this comment has already been forwarded to work "
                            f"order `{self._delivery.run_id}`, and one comment "
                            "is forwarded once"
                        )
                    self._delivery = Delivery(attempted=True)
                    reached = await self.continue_workorder(origin, prompt)
                    self._delivery = Delivery(
                        run_id=reached.run_id, url=reached.url, attempted=True,
                        started=reached.started,
                    )
                    # Recorded here rather than once the turn ends, because the
                    # work order already has the feedback: everything after this
                    # point, including the rest of the turn, is a step that may
                    # fail and be retried.
                    self._remember_forwarded()
                    return reached

                broker = await opened.enter_async_context(
                    FeedbackBroker(continue_workorder=continue_workorder)
                )
                client = await self.provider.connect()
                opened.push_async_callback(client.close)
                session = await client.new_session(cwd=cwd, mcp_servers=[broker.config])
                self._threads[key] = (opened, session)
            except BaseException:
                await opened.aclose()
                raise
        self._threads.move_to_end(key)
        session = self._threads[key][1]
        prompt = (INSTRUCTIONS + "\nUser: " if fresh else "") + request.text
        # Driven to completion and discarded. The turn's worth is in what it
        # asked of the tool, which has already been recorded by the time this
        # finishes; its prose is the part an untrusted commenter can dictate,
        # and reading it here is what would give that prose somewhere to go.
        async for _event in session.prompt(prompt):
            pass
        return {"reply": self._delivery.announcement()}

    async def _reply(self, state: ConversationState) -> dict:
        await self.reply(state["request"].origin, state["reply"])
        return {}


__all__ = [
    "AlreadyForwarded",
    "Continuation",
    "Delivery",
    "FeedbackRequest",
    "GithubConcierge",
    "build_graph",
]
