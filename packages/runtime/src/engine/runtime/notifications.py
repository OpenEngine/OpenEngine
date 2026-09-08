"""Reporting a run's progress back to the conversation that asked for it.

A run started from a chat message has somewhere to answer: the thread the
request arrived in. This is the one place that knows how to get back there, so
the executor and the run-bound tool server each say what happened rather than
each working out where to say it.

Delivery is best effort where nobody is waiting on it. A chat provider that is
unreachable, not connected, or refusing the channel must not fail the work it
was reporting on -- the run is the thing that matters, and its record lives in
the store either way.

`deliver` is the exception, and exists because `update_status` has somebody
waiting: the agent that called it. Swallowing a failure there would answer
"status posted" to a step whose status went nowhere, and an agent reading that
as confirmation will not mention the gap or try again.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from engine.domain import RunOrigin, RunState
from engine.ports import Communications, Message, MessageLink

logger = logging.getLogger(__name__)


class RunNotifier:
    """Post a run's progress into the conversation it came from."""

    def __init__(self, communications: Communications, public_url: str = "") -> None:
        self._communications = communications
        self._public_url = public_url.rstrip("/")

    def work_order_link(self, state: RunState) -> MessageLink | None:
        """Where a person goes to watch this run, if this deployment has a URL."""
        if not self._public_url:
            return None
        return MessageLink("View work order", f"{self._public_url}/runs/{state.run_id}")

    async def announce(
        self,
        state: RunState,
        text: str,
        *,
        links: Iterable[MessageLink] = (),
        mention: bool = False,
    ) -> None:
        """Say something in this run's thread, if it has one.

        A run created from the web has no origin and nothing to say to, so this
        is a no-op rather than a fallback to some configured channel: an update
        addressed to nobody in particular is noise in somebody else's room.

        The executor announces on a run's behalf, with nobody waiting on the
        answer, so a failure is logged rather than raised. Use `deliver` where
        there is somebody to tell.
        """
        try:
            await self.deliver(state, text, links=links, mention=mention)
        except Exception:
            logger.exception("could not report progress for run %s", state.run_id)

    async def deliver(
        self,
        state: RunState,
        text: str,
        *,
        links: Iterable[MessageLink] = (),
        mention: bool = False,
    ) -> None:
        """Say something in this run's thread, and let a failure through.

        Same message as `announce`; the difference is only who answers for it.
        A run with no origin is still a no-op -- there was nothing to deliver,
        which is not a failure to report.
        """
        origin = state.origin
        if origin is None or not origin.channel:
            return
        await self._send(
            origin,
            Message(
                text,
                tuple(links),
                origin.author if mention and origin.author else "",
            ),
            state,
        )

    async def post(
        self, origin: RunOrigin, message: Message, state: RunState | None = None
    ) -> None:
        """Send a message to an origin directly, best effort."""
        try:
            await self._send(origin, message, state)
        except Exception:
            logger.exception(
                "could not report progress for run %s",
                state.run_id if state is not None else origin.channel,
            )

    async def _send(
        self, origin: RunOrigin, message: Message, state: RunState | None
    ) -> None:
        await self._communications.post(
            origin.channel,
            message,
            state.run_id if state is not None else None,
            thread_id=origin.thread_id,
        )


__all__ = ["RunNotifier"]
