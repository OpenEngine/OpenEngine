"""The server adapter's envelope checks, authorization and replies, request bytes in, reply bytes out."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from openengine.messaging import LocalOperator, Request, Route, Server, ServiceError, envelopes, listen_address

LOOPBACK = "nats://127.0.0.1:4222"
GRAPHS = "graph_engine/v1/schemas/queries.schema.json#/$defs/list_graphs_query"


async def empty_page(request: Request) -> dict[str, Any]:
    return {"items": [], "next_cursor": None}


async def accept(request: Request) -> dict[str, Any]:
    return {"command_id": request.body["command_id"], "status": "accepted", "operation_id": "op_1"}


def _server(listener: str = LOOPBACK) -> Server:
    return Server(
        None, "ge", listener,
        queries={"graphs.list": Route(empty_page, GRAPHS)},
        commands={"graphs.apply": Route(accept)},
    )


def _send(subject: str, body: Any, listener: str = LOOPBACK) -> dict[str, Any]:
    data = body if isinstance(body, bytes) else json.dumps(body).encode()
    return json.loads(asyncio.run(_server(listener).handle(subject, data)))


def test_a_valid_query_gets_its_result_with_its_request_id() -> None:
    reply = _send("oe.v1.ge.query.graphs.list", {"request_id": "req_1", "params": {}})

    assert reply == {"request_id": "req_1", "status": "ok", "result": {"items": [], "next_cursor": None}}
    assert not envelopes.violations(envelopes.QUERY_RESPONSE, reply)


@pytest.mark.parametrize(
    ("body", "field"),
    [
        (b"not json", "/"),
        ({"request_id": "req_1"}, "/"),
        ({"params": {}, "actor": "someone"}, "/"),
        ({"params": {"limit": 1000}}, "/params/limit"),
    ],
)
def test_a_malformed_query_is_invalid_argument(body: Any, field: str) -> None:
    reply = _send("oe.v1.ge.query.graphs.list", body)

    assert reply["status"] == "error"
    assert reply["error"]["code"] == "INVALID_ARGUMENT"
    assert reply["error"]["retryable"] is False
    assert field in [violation["field"] for violation in reply["error"]["details"]["violations"]]
    assert not envelopes.violations(envelopes.QUERY_RESPONSE, reply)


def test_a_command_whose_causal_path_does_not_end_with_its_id_is_invalid_argument() -> None:
    reply = _send("oe.v1.ge.control.graphs.apply", {"command_id": "cmd_1", "causal_path": ["cmd_0"], "payload": {}})

    assert reply["command_id"] == "cmd_1"
    assert reply["status"] == "rejected"
    assert reply["error"]["code"] == "INVALID_ARGUMENT"
    assert not envelopes.violations(envelopes.COMMAND_RESPONSE, reply)


def test_a_valid_command_gets_its_handlers_response() -> None:
    reply = _send("oe.v1.ge.control.graphs.apply", envelopes.command({}, command_id="cmd_1"))

    assert reply == {"command_id": "cmd_1", "status": "accepted", "operation_id": "op_1"}


def test_a_request_through_a_non_loopback_listener_is_permission_denied() -> None:
    reply = _send("oe.v1.ge.query.graphs.list", {"params": {}}, listener="nats://10.0.0.5:4222")

    assert reply["error"]["code"] == "PERMISSION_DENIED"
    assert reply["error"]["details"] == {"permission": "query.graphs.list"}


@pytest.mark.parametrize("listener", ["nats://127.0.0.1:4222", "nats://localhost:4222", "nats://[::1]:4222"])
def test_local_operator_mode_allows_loopback_listeners(listener: str) -> None:
    LocalOperator().authorize(Request("oe.v1.ge.query.graphs.list", "query", "graphs.list", {"params": {}}, listener))


def test_an_unserved_subject_is_unsupported_capability() -> None:
    reply = _send("oe.v1.ge.query.jobs.list", {"params": {}})

    assert reply["error"]["code"] == "UNSUPPORTED_CAPABILITY"


def test_a_handler_fault_is_internal_without_its_detail() -> None:
    detail = "secret detail"

    async def broken(request: Request) -> dict[str, Any]:
        raise RuntimeError(detail)

    server = Server(None, "ge", LOOPBACK, queries={"graphs.list": Route(broken)})
    reply = json.loads(asyncio.run(server.handle("oe.v1.ge.query.graphs.list", b'{"params": {}}')))

    assert reply["error"]["code"] == "INTERNAL"
    assert "secret" not in reply["error"]["message"]


def test_a_causal_path_past_its_limit_is_refused_before_sending() -> None:
    with pytest.raises(ServiceError) as raised:
        envelopes.command({}, command_id="cmd_33", caused_by=[f"cmd_{index}" for index in range(32)])

    assert raised.value.code == "CAUSAL_PATH_TOO_LONG"


@pytest.mark.parametrize(
    ("info", "address", "allowed"),
    [
        ({"host": "127.0.0.1", "port": 4222}, "nats://127.0.0.1:4222", True),
        ({"host": "0.0.0.0", "port": 4222}, "nats://0.0.0.0:4222", False),
        ({"host": "::", "port": 4222}, "nats://[::]:4222", False),
    ],
)
def test_local_operator_mode_judges_the_address_nats_listens_on(info: dict[str, Any], address: str, allowed: bool) -> None:
    connection = type("Connected", (), {"_server_info": info})()

    assert listen_address(connection) == address
    reply = _send("oe.v1.ge.query.graphs.list", {"params": {}}, listener=address)
    assert (reply["status"] == "ok") is allowed
