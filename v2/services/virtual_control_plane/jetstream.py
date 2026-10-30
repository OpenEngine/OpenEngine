"""Publish VCP's public events to the existing `vcp-events` JetStream stream."""

from __future__ import annotations

import json
import re
from functools import cache
from typing import Any

from nats.js import JetStreamContext

from openengine.contracts.validate import Contract, load_contract, validate_message

STREAM = "vcp-events"
EVENTS = "oe.v1.vcp.events.workspaces"
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
    return f"{EVENTS}.{workspace_id}.{event_type}"


@cache
def _events() -> tuple[Contract, dict[str, str]]:
    contract = load_contract("virtual_control_plane")
    prefix = EVENTS + ".{workspace_id}."
    return contract, {
        message.address.removeprefix(prefix): name for name, message in contract.messages.items()
        if message.address is not None and message.address.startswith(prefix)
    }


def event_errors(envelope: Any) -> list[str]:
    """Every way `envelope` breaks the public contract of the event it would be published as.

    This covers the schema and ID formats, the causal-path rule, the subject
    and the `Nats-Msg-Id` header, so an event that passes can be addressed.
    """
    if not isinstance(envelope, dict):
        return ["an event envelope must be an object"]
    contract, messages = _events()
    event_type = envelope.get("event_type")
    if not isinstance(event_type, str) or event_type not in messages:
        return [f"{event_type!r} is not a VCP workspace event type"]
    resources = envelope.get("resources")
    workspace_id = resources.get("workspace_id") if isinstance(resources, dict) else None
    return validate_message(
        contract, messages[event_type], envelope,
        subject=f"{EVENTS}.{workspace_id}.{event_type}", headers={"Nats-Msg-Id": envelope.get("event_id")},
    )


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
