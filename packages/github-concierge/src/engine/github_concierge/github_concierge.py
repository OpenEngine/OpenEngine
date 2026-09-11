"""Pull-request conversation graph. Transport and work-order steering injected.

A pull-request conversation is not a Slack thread with a different address: the
concierge here may only steer the work order that already opened the pull
request, and its participants are whoever can comment on it rather than one
person in a direct message. Both differences are authority, so this is its own
graph rather than a mode of the Slack one.
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
from langgraph_acp.events import ACPEventType
from langgraph_acp.session import ACPSession

from .github_egress import FeedbackBroker

#: Given the origin that asked and the feedback, reach that pull request's work
#: order and return (url, run_id).
SteerWorkorder = Callable[[RunOrigin, str], Awaitable[tuple[str, str]]]
Reply = Callable[[RunOrigin, str], Awaitable[None]]

INSTRUCTIONS = """You are OpenEngineBot, a pull request concierge. Reply briefly to
questions about this pull request. When someone asks for a change or for
review feedback to be addressed, use continue_workorder with their request: it
forwards the request to the work order that opened this pull request. You have
no implementation role and cannot start work orders: use only the granted
continue_workorder tool. Never claim feedback was delivered unless the tool
succeeds.
"""


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

    def __init__(self, *, provider: ACPAgentProvider, steer_workorder: SteerWorkorder,
                 reply: Reply, max_threads: int = 32,
                 timeout_seconds: float = 180) -> None:
        if max_threads < 1:
            raise ValueError("max_threads must be positive")
        self.provider = provider
        self.steer_workorder = steer_workorder
        self.reply = reply
        self.max_threads = max_threads
        self.timeout_seconds = timeout_seconds
        self._threads: OrderedDict[
            tuple[str, str, str], tuple[AsyncExitStack, ACPSession]
        ] = OrderedDict()
        self._lock = asyncio.Lock()
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

    async def _turn(self, state: ConversationState) -> dict[str, str]:
        request = state["request"]
        origin = request.origin
        key = self._key(origin)
        fresh = key not in self._threads
        if fresh:
            while len(self._threads) >= self.max_threads:
                await self._forget(next(iter(self._threads)))
            opened = AsyncExitStack()
            try:
                cwd = opened.enter_context(TemporaryDirectory(prefix="github-concierge-"))

                async def steer(prompt: str) -> tuple[str, str]:
                    # The session belongs to this origin for its whole life, so
                    # the authority a tool call carries is the authority of the
                    # author whose history it was reasoning over.
                    return await self.steer_workorder(origin, prompt)

                broker = await opened.enter_async_context(
                    FeedbackBroker(steer_workorder=steer)
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
        parts = []
        async for event in session.prompt(prompt):
            if event.type == ACPEventType.MESSAGE_DELTA:
                content = event.data.get("content", {})
                if isinstance(content, dict) and content.get("type") == "text":
                    parts.append(str(content.get("text", "")))
        return {"reply": "".join(parts).strip() or "I'm working on that."}

    async def _reply(self, state: ConversationState) -> dict:
        await self.reply(state["request"].origin, state["reply"])
        return {}


__all__ = ["FeedbackRequest", "GithubConcierge", "build_graph"]
