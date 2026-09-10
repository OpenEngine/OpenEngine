"""Conversation graph. Slack transport and work-order execution are injected."""
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
from langgraph_acp.events import ACPEventType

from .slack_egress import ConciergeBroker

CreateWorkorder = Callable[[RunOrigin, str, str], Awaitable[tuple[str, str]]]
Reply = Callable[[RunOrigin, str], Awaitable[None]]

INSTRUCTIONS = """You are OpenEngineBot, a Slack concierge. For a greeting or test
message respond 'Hi, how can I help?'. Only when the user requests work, use
create_workorder with their task and repository (ask if no default is available).
Do not claim work started unless the tool succeeds. Work-order progress and its
UI link are posted in this thread by the host. Keep replies brief. You have no
implementation role: use only the granted create_workorder tool.
"""


@dataclass(frozen=True)
class IncomingMessage:
    origin: RunOrigin
    text: str
    message_ts: str = ""


class ConversationState(TypedDict):
    message: IncomingMessage
    reply: str


def build_graph(
    turn: Callable[[ConversationState], Awaitable[dict]],
    reply: Callable[[ConversationState], Awaitable[dict]],
):
    """START -> ACP conversation turn -> Slack reply -> END."""
    graph = StateGraph(ConversationState)
    graph.add_node("concierge", turn)
    graph.add_node("reply", reply)
    graph.add_edge(START, "concierge")
    graph.add_edge("concierge", "reply")
    graph.add_edge("reply", END)
    return graph.compile()


class SlackConcierge:
    """Bounded live ACP sessions; handle, has_thread, forget, and close are the API.

    Turns are serialized to prevent concurrent session creation and eviction of
    an active session. Conversation state is intentionally process-local.
    """

    def __init__(self, *, provider: ACPAgentProvider, create_workorder: CreateWorkorder,
                 reply: Reply, default_repository: str = "", max_threads: int = 32,
                 timeout_seconds: float = 180) -> None:
        if max_threads < 1:
            raise ValueError("max_threads must be positive")
        self.provider = provider
        self.create_workorder = create_workorder
        self.reply = reply
        self.default_repository = default_repository
        self.max_threads = max_threads
        self.timeout_seconds = timeout_seconds
        self._threads: OrderedDict[tuple[str, str], tuple[AsyncExitStack, ACPSession]] = OrderedDict()
        self._lock = asyncio.Lock()
        self.graph = build_graph(self._turn, self._reply)

    def has_thread(self, channel: str, thread_id: str) -> bool:
        return (channel, thread_id) in self._threads

    async def _forget(self, key: tuple[str, str]) -> None:
        item = self._threads.pop(key, None)
        if item is not None:
            await item[0].aclose()

    async def forget(self, channel: str, thread_id: str) -> None:
        async with self._lock:
            await self._forget((channel, thread_id))

    async def close(self) -> None:
        async with self._lock:
            for key in list(self._threads):
                await self._forget(key)

    async def handle(self, message: IncomingMessage) -> None:
        async with self._lock:
            key = (message.origin.channel, message.origin.thread_id)
            try:
                async with asyncio.timeout(self.timeout_seconds):
                    await self.graph.ainvoke({"message": message})
            except BaseException:
                await self._forget(key)
                raise

    async def _turn(self, state: ConversationState) -> dict[str, str]:
        message = state["message"]
        key = (message.origin.channel, message.origin.thread_id)
        fresh = key not in self._threads
        if fresh:
            while len(self._threads) >= self.max_threads:
                await self._forget(next(iter(self._threads)))
            opened = AsyncExitStack()
            try:
                cwd = opened.enter_context(TemporaryDirectory(prefix="slack-concierge-"))
                async def create(repository: str, prompt: str) -> tuple[str, str]:
                    return await self.create_workorder(message.origin, repository, prompt)
                broker = await opened.enter_async_context(ConciergeBroker(
                    create_workorder=create, default_repository=self.default_repository))
                client = await self.provider.connect()
                opened.push_async_callback(client.close)
                session = await client.new_session(cwd=cwd, mcp_servers=[broker.config])
                self._threads[key] = (opened, session)
            except BaseException:
                await opened.aclose()
                raise
        self._threads.move_to_end(key)
        session = self._threads[key][1]
        prompt = (INSTRUCTIONS + "\nUser: " if fresh else "") + message.text
        parts = []
        async for event in session.prompt(prompt):
            if event.type == ACPEventType.MESSAGE_DELTA:
                content = event.data.get("content", {})
                if isinstance(content, dict) and content.get("type") == "text":
                    parts.append(str(content.get("text", "")))
        return {"reply": "".join(parts).strip() or "I'm working on that."}

    async def _reply(self, state: ConversationState) -> dict:
        await self.reply(state["message"].origin, state["reply"])
        return {}
