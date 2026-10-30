"""The client's retries, timeouts and error mapping, against a scripted NATS."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

import pytest
from nats.errors import ConnectionClosedError, NoRespondersError, TimeoutError as NatsTimeoutError

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
    nats = ScriptedNats([{"request_id": "req_error", "status": "error", "error": error}])

    with pytest.raises(ServiceError) as raised:
        asyncio.run(Client(nats).query("oe.v1.ge.query.graphs.graph_review.get", request_id="req_error"))

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


def test_a_connection_lost_while_waiting_reports_unavailable() -> None:
    nats = ScriptedNats([NatsTimeoutError(), ConnectionClosedError()])

    with pytest.raises(Unavailable) as raised:
        asyncio.run(Client(nats, timeout=0.25).query("oe.v1.ge.query.graphs.list"))

    assert raised.value.code is ErrorCode.UNAVAILABLE
    assert len(nats.sent) == 2


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), True, "2", 61])
def test_invalid_timeout_is_refused(timeout) -> None:
    with pytest.raises(ValueError, match="timeout"):
        Client(ScriptedNats([]), timeout=timeout)


@pytest.mark.parametrize("attempts", [0, -1, 1.5, True, "3", 11])
def test_invalid_attempts_are_refused(attempts) -> None:
    with pytest.raises(ValueError, match="attempts"):
        Client(ScriptedNats([]), attempts=attempts)


@pytest.mark.parametrize("reply", [
    None, [], "text", {},
    {"request_id": "req_1", "status": "ok"},
    {"request_id": "req_other", "status": "ok", "result": {}},
    {"status": "ok", "result": {}},
    {"request_id": "req_1", "status": "ok", "result": []},
    {"request_id": "req_1", "status": "ok", "result": {}, "error": {}},
    {"request_id": "req_1", "status": "error", "error": {"code": [], "message": "bad", "retryable": False}},
    {"request_id": "req_1", "status": "error", "error": {"code": "NEW", "message": "bad", "retryable": "false"}},
])
def test_malformed_query_response_is_actionable_internal_error(reply) -> None:
    nats = ScriptedNats([reply])
    with pytest.raises(ServiceError, match="oe logs") as raised:
        asyncio.run(Client(nats).query("oe.v1.ge.query.graphs.list", request_id="req_1"))
    assert raised.value.code == ErrorCode.INTERNAL
    assert not raised.value.retryable
    assert len(nats.sent) == 1


@pytest.mark.parametrize("reply", [
    {"command_id": "cmd_other", "status": "accepted", "operation_id": "op_1"},
    {"command_id": "cmd_1", "status": "accepted"},
    {"command_id": "cmd_1", "status": "accepted", "operation_id": "invalid"},
    {"command_id": "cmd_1", "status": "rejected", "error": {}},
])
def test_malformed_command_response_is_internal_error(reply) -> None:
    with pytest.raises(ServiceError, match="invalid service response"):
        asyncio.run(Client(ScriptedNats([reply])).command(SUBJECT, {}, command_id="cmd_1"))


def test_additive_reply_fields_and_unknown_error_codes_are_tolerated() -> None:
    reply = {"request_id": "req_1", "status": "error", "future": True,
             "error": {"code": "NEW", "message": "new failure", "retryable": True, "future": []}}
    with pytest.raises(ServiceError) as raised:
        asyncio.run(Client(ScriptedNats([reply])).query("query", request_id="req_1"))
    assert raised.value.code == "NEW"
    assert raised.value.retryable is True


@pytest.mark.parametrize("data", [b"not json", b"\xff"])
def test_invalid_json_response_is_internal_error(data) -> None:
    class RawNats:
        async def request(self, *args, **kwargs):
            return Reply(data)

    with pytest.raises(ServiceError, match="oe logs") as raised:
        asyncio.run(Client(RawNats()).query("query"))
    assert raised.value.code == ErrorCode.INTERNAL



def test_known_error_retryable_flag_is_authoritative() -> None:
    reply = {"command_id": "cmd_1", "status": "rejected",
             "error": {"code": "PERMISSION_DENIED", "message": "retry later", "retryable": True}}
    with pytest.raises(ServiceError) as raised:
        asyncio.run(Client(ScriptedNats([reply])).command(SUBJECT, {}, command_id="cmd_1"))
    assert raised.value.code == "PERMISSION_DENIED"
    assert raised.value.retryable is True
