"""`virtual_control_plane.__main__`'s wiring: authorization runs before any handler,
a non-loopback broker is refused even though VCP dialed it over loopback, the one
query it serves is answered truthfully, and the state directory lock turns a second
process into a reported startup failure rather than a silent race."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from openengine.messaging import LOCAL_TENANT, LocalOperator, Principal, Request, Server, ServiceError, envelopes

from virtual_control_plane import __main__ as vcp
from virtual_control_plane.exclusion import StateDirectory

LOOPBACK = "nats://127.0.0.1:4222"


def _send(subject: str, body: Any, *, authorizer=None, listener: str = LOOPBACK) -> dict[str, Any]:
    server = Server(
        None, "vcp", listener, queries=vcp.QUERIES,
        **({"authorizer": authorizer} if authorizer is not None else {}),
    )
    data = json.dumps(body).encode()
    return json.loads(asyncio.run(server.handle(subject, data)))


class _Rejecting:
    def authorize(self, request: Request) -> Principal:
        raise ServiceError("PERMISSION_DENIED", "no")


def test_capabilities_get_truthfully_reports_no_providers() -> None:
    reply = _send("oe.v1.vcp.query.capabilities.get", {"params": {}})

    assert reply == {"status": "ok", "result": {"providers": []}}
    assert not envelopes.violations(envelopes.QUERY_RESPONSE, reply)


def test_a_rejecting_authorizer_prevents_the_handler_from_running(monkeypatch: pytest.MonkeyPatch) -> None:
    called = False

    async def spy(request: Request) -> dict[str, Any]:
        nonlocal called
        called = True
        return {"providers": []}

    monkeypatch.setitem(vcp.QUERIES, "capabilities.get", vcp.Route(spy, vcp.QUERIES["capabilities.get"].schema))

    reply = _send("oe.v1.vcp.query.capabilities.get", {"params": {}}, authorizer=_Rejecting())

    assert reply["status"] == "error"
    assert reply["error"]["code"] == "PERMISSION_DENIED"
    assert called is False


def test_local_operator_refuses_a_broker_listening_beyond_loopback() -> None:
    reply = _send("oe.v1.vcp.query.capabilities.get", {"params": {}}, listener="nats://0.0.0.0:4222")

    assert reply["error"]["code"] == "PERMISSION_DENIED"


def test_local_operator_allows_a_loopback_broker_as_the_local_tenant() -> None:
    principal = LocalOperator().authorize(Request("oe.v1.vcp.query.capabilities.get", "query", "capabilities.get", {}, LOOPBACK))

    assert principal == Principal(LOCAL_TENANT)


def test_main_fails_startup_rather_than_racing_a_running_vcp_for_the_state_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(vcp.STATE_DIR_VARIABLE, str(tmp_path))
    holder = StateDirectory(tmp_path)
    holder.acquire()
    try:
        assert vcp.main() == vcp.EXIT_FAILED
    finally:
        holder.release()
