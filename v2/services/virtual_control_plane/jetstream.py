"""Publish VCP's public events to the existing `vcp-events` JetStream stream."""

from __future__ import annotations

import json
import re
from typing import Any

from nats.js import JetStreamContext

STREAM = "vcp-events"
PUBLISH_TIMEOUT_SECONDS = 5.0
# A subject token must not split the subject or act as a wildcard.
_TOKEN = re.compile(r"[^.*>\s]+")


class UnaddressableEvent(ValueError):
    def __init__(self, event_id: str) -> None:
        super().__init__(f"cannot address event {event_id} by subject")


def event_subject(envelope: dict[str, Any]) -> str:
    """`oe.v1.vcp.events.workspaces.{workspace_id}.{event_type}`, the one partition VCP's events use."""
    workspace_id, event_type = envelope["resources"]["workspace_id"], envelope["event_type"]
    if not _TOKEN.fullmatch(workspace_id) or not all(_TOKEN.fullmatch(part) for part in event_type.split(".")):
        raise UnaddressableEvent(envelope["event_id"])
    return f"oe.v1.vcp.events.workspaces.{workspace_id}.{event_type}"


class JetStreamEvents:
    """An `EventPublisher` that waits for the stream's acknowledgement.

    `Nats-Msg-Id` is the event ID, so a retry within the stream's duplicate
    window is acknowledged without being stored twice.
    """

    def __init__(self, jetstream: JetStreamContext, timeout: float = PUBLISH_TIMEOUT_SECONDS) -> None:
        self._jetstream = jetstream
        self._timeout = timeout

    async def publish(self, event: dict[str, Any]) -> None:
        await self._jetstream.publish(
            event_subject(event),
            json.dumps(event, separators=(",", ":")).encode(),
            timeout=self._timeout,
            stream=STREAM,
            headers={"Nats-Msg-Id": event["event_id"]},
        )
