"""The client's retries, timeouts and error mapping, against a scripted NATS."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

import pytest
from nats.errors import NoRespondersError, TimeoutError as NatsTimeoutError

from openengine.messaging import Client, ErrorCode, ServiceError, Unavailable


@dataclass
class Reply:
    data: bytes


@dataclass
class ScriptedNats:
    """Answers each request with the next of `outcomes`: a reply body, or an exception to raise."""

    outcomes: list[Any]
    sent: list[tuple[str, bytes, float]] = field(default_factory=list)

    async def request(self, subject: str, payload: bytes = b"", timeout: float = 0.5, old_style: bool = False,
                      headers: dict[str, Any] | None = None) -> Reply:
        self.sent.append((subject, payload, timeout))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return Reply(json.dumps(outcome).encode())


SUBJECT = "oe.v1.ge.control.graphs.apply"


def test_a_command_whose_reply_was_lost_is_resent_with_the_same_command_id() -> None:
    accepted = {"command_id": "cmd_1", "status": "accepted", "operation_id": "op_1"}
    nats = ScriptedNats([NatsTimeoutError(), accepted])

    reply = asyncio.run(Client(nats, timeout=0.25).command(SUBJECT, {"name": "review"}, command_id="cmd_1", caused_by=["evt_0"]))

    assert reply == accepted
    (_, first, _), (_, retry, _) = nats.sent
    assert retry == first
    body = json.loads(retry)
    assert body["command_id"] == "cmd_1"
    assert body["causal_path"] == ["evt_0", "cmd_1"]


def test_each_attempt_waits_the_bounded_timeout_then_reports_unavailable() -> None:
    nats = ScriptedNats([NatsTimeoutError()] * 3)

    with pytest.raises(Unavailable) as raised:
        asyncio.run(Client(nats, timeout=0.25, attempts=3).query("oe.v1.ge.query.graphs.list"))

    assert [timeout for _, _, timeout in nats.sent] == [0.25] * 3
    assert len({payload for _, payload, _ in nats.sent}) == 1
    assert raised.value.code == ErrorCode.UNAVAILABLE
    assert raised.value.retryable


def test_no_responders_is_reported_at_once() -> None:
    nats = ScriptedNats([NoRespondersError()])

    with pytest.raises(Unavailable) as raised:
        asyncio.run(Client(nats).query("oe.v1.ge.query.graphs.list"))

    assert raised.value.no_responders
    assert len(nats.sent) == 1


def test_an_error_reply_raises_its_code_and_keeps_its_retryable_flag() -> None:
    error = {"code": "NOT_FOUND", "message": "no graph review", "retryable": True}
    nats = ScriptedNats([{"status": "error", "error": error}])

    with pytest.raises(ServiceError) as raised:
        asyncio.run(Client(nats).query("oe.v1.ge.query.graphs.graph_review.get"))

    assert raised.value.code == ErrorCode.NOT_FOUND
    assert raised.value.retryable is True
    assert raised.value.to_dict() == error


def test_an_unknown_error_code_is_kept_as_sent() -> None:
    nats = ScriptedNats([{"command_id": "cmd_1", "status": "rejected",
                          "error": {"code": "NEWER_CODE", "message": "later release", "retryable": False}}])

    with pytest.raises(ServiceError) as raised:
        asyncio.run(Client(nats).command(SUBJECT, {}, command_id="cmd_1"))

    assert raised.value.code == "NEWER_CODE"
    assert raised.value.retryable is False


def test_a_query_is_sent_as_a_valid_envelope() -> None:
    nats = ScriptedNats([{"request_id": "req_1", "status": "ok", "result": {"items": [], "next_cursor": None}}])

    asyncio.run(Client(nats).query("oe.v1.ge.query.graphs.list", {"limit": 10}, request_id="req_1"))

    assert json.loads(nats.sent[0][1]) == {"request_id": "req_1", "params": {"limit": 10}}
