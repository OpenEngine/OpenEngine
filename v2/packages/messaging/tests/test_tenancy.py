"""Tenancy modes: which tenant a server attaches to a request, the queue group it joins, and how a client addresses one."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from openengine.messaging import (
    LOCAL_TENANT,
    CachedResolver,
    Client,
    Dedicated,
    InMemoryAccounts,
    LocalOperator,
    Mode,
    Principal,
    Request,
    Route,
    Server,
    Shared,
)
from openengine.messaging.tenancy import shared_subject, subject_template

LOOPBACK = "nats://127.0.0.1:4222"
ACCOUNT = "A" + "B" * 55
OTHER_ACCOUNT = "A" + "C" * 55
TENANT = "0192f1a0-5b3c-7def-8abc-0123456789ab"
NIL = "00000000-0000-0000-0000-000000000000"
LIST = "oe.v1.ge.query.graphs.list"


@dataclass
class CountingResolver:
    tenants: dict[str, str] = field(default_factory=dict)
    asked: list[str] = field(default_factory=list)

    async def resolve(self, account: str) -> str | None:
        self.asked.append(account)
        return self.tenants.get(account)


@dataclass
class Subscribing:
    """Records what a server subscribes to, as nats-py's `subscribe` would be called."""

    subscriptions: list[tuple[str, str]] = field(default_factory=list)

    async def subscribe(self, subject: str, queue: str = "", cb: Any = None) -> object:
        self.subscriptions.append((subject, queue))
        return object()

    async def flush(self) -> None:
        pass


def _server(tenancy: Any = None, **routes: Route) -> tuple[Server, list[Request]]:
    seen: list[Request] = []

    async def record(request: Request) -> dict[str, Any]:
        seen.append(request)
        return {"items": [], "next_cursor": None}

    server = Server(
        None, "ge", LOOPBACK, queries={"graphs.list": Route(record), **routes},
        **({"tenancy": tenancy} if tenancy is not None else {}),
    )
    return server, seen


def _send(server: Server, subject: str) -> dict[str, Any]:
    return json.loads(asyncio.run(server.handle(subject, json.dumps({"params": {}}).encode())))


def test_dedicated_mode_attaches_local_tenant_by_default() -> None:
    server, seen = _server()

    reply = _send(server, LIST)

    assert reply["status"] == "ok"
    assert seen[0].principal == Principal(LOCAL_TENANT)
    assert seen[0].principal.account is None
    assert server.subjects == ["oe.v1.ge.query.>", "oe.v1.ge.control.>"]


def test_dedicated_mode_attaches_the_configured_tenant() -> None:
    server, seen = _server(Dedicated(TENANT))

    assert _send(server, LIST)["status"] == "ok"
    assert seen[0].principal == Principal(TENANT)


def test_shared_mode_resolves_the_third_token_account_and_strips_it_for_routing() -> None:
    server, seen = _server(Shared(InMemoryAccounts({ACCOUNT: TENANT})))

    reply = _send(server, f"oe.v1.{ACCOUNT}.ge.query.graphs.list")

    assert reply["status"] == "ok"
    assert seen[0].principal == Principal(TENANT, ACCOUNT)
    assert (seen[0].kind, seen[0].operation) == ("query", "graphs.list")
    assert server.subjects == ["oe.v1.*.ge.query.>", "oe.v1.*.ge.control.>"]


def test_shared_mode_keeps_subject_parameters_after_the_account_token() -> None:
    async def operation(request: Request) -> dict[str, Any]:
        return {"parameters": dict(request.parameters), "tenant_id": request.principal.tenant_id}

    server, _ = _server(Shared(InMemoryAccounts({ACCOUNT: TENANT})), **{"operations.{operation_id}.get": Route(operation)})

    reply = _send(server, f"oe.v1.{ACCOUNT}.ge.query.operations.op_1.get")

    assert reply["result"] == {"parameters": {"operation_id": "op_1"}, "tenant_id": TENANT}


@pytest.mark.parametrize("account", [OTHER_ACCOUNT, "not-an-account", "", "A" + "b" * 55])
def test_shared_mode_refuses_an_unknown_or_malformed_account_with_permission_denied(account: str) -> None:
    server, seen = _server(Shared(InMemoryAccounts({ACCOUNT: TENANT})))

    reply = _send(server, f"oe.v1.{account}.ge.query.graphs.list")

    assert reply["status"] == "error"
    assert reply["error"]["code"] == "PERMISSION_DENIED"
    assert reply["error"]["details"] == {"permission": "query.graphs.list"}
    assert not account or account not in reply["error"]["message"]
    assert seen == []


def test_shared_mode_caches_a_resolved_account_but_not_a_refusal() -> None:
    resolver = CountingResolver({ACCOUNT: TENANT})
    server, seen = _server(Shared(resolver))

    for _ in range(3):
        assert _send(server, f"oe.v1.{ACCOUNT}.ge.query.graphs.list")["status"] == "ok"
    for _ in range(2):
        assert _send(server, f"oe.v1.{OTHER_ACCOUNT}.ge.query.graphs.list")["error"]["code"] == "PERMISSION_DENIED"

    assert resolver.asked == [ACCOUNT, OTHER_ACCOUNT, OTHER_ACCOUNT]
    assert isinstance(server.tenancy.resolver, CachedResolver)
    assert [request.principal.tenant_id for request in seen] == [TENANT] * 3


def test_a_resolver_answering_the_nil_uuid_is_rejected_before_any_handler_runs() -> None:
    server, seen = _server(Shared(InMemoryAccounts({ACCOUNT: NIL})))

    reply = _send(server, f"oe.v1.{ACCOUNT}.ge.query.graphs.list")

    assert reply["error"]["code"] == "INTERNAL"
    assert seen == []


@pytest.mark.parametrize("tenant_id", [NIL, "", "not-a-uuid", TENANT.upper(), "0192f1a0-5b3c-4def-8abc-0123456789ab"])
def test_a_nil_or_malformed_tenant_id_is_rejected_at_config_time(tenant_id: str) -> None:
    with pytest.raises(ValueError):
        Dedicated(tenant_id)
    with pytest.raises(ValueError):
        Principal(tenant_id)


def test_both_modes_subscribe_in_a_queue_group_named_after_the_service() -> None:
    for tenancy in (Dedicated(), Shared(InMemoryAccounts())):
        nats = Subscribing()
        server = Server(nats, "ge", LOOPBACK, tenancy=tenancy)

        asyncio.run(server.start())

        assert [queue for _, queue in nats.subscriptions] == ["ge", "ge"]
        assert [subject for subject, _ in nats.subscriptions] == server.subjects


def test_the_authorizer_sees_the_derived_principal_and_its_answer_is_attached() -> None:
    class Narrowing:
        def __init__(self) -> None:
            self.seen: list[Principal | None] = []

        def authorize(self, request: Request) -> Principal:
            self.seen.append(request.principal)
            return Principal(request.principal.tenant_id, ACCOUNT)

    authorizer = Narrowing()
    server, seen = _server(Dedicated(TENANT))
    server.authorizer = authorizer

    assert _send(server, LIST)["status"] == "ok"
    assert authorizer.seen == [Principal(TENANT)]
    assert seen[0].principal == Principal(TENANT, ACCOUNT)


def test_local_operator_returns_local_tenant_for_a_request_built_by_hand() -> None:
    assert LocalOperator().authorize(Request(LIST, "query", "graphs.list", {}, LOOPBACK)) == Principal(LOCAL_TENANT)


@dataclass
class Reply:
    data: bytes


@dataclass
class RecordingNats:
    sent: list[str] = field(default_factory=list)

    async def request(self, subject: str, payload: bytes = b"", timeout: float = 0.5, old_style: bool = False,
                      headers: dict[str, Any] | None = None) -> Reply:
        self.sent.append(subject)
        return Reply(json.dumps({"status": "ok", "result": {}}).encode())


def test_a_shared_mode_client_addresses_the_principals_account() -> None:
    nats = RecordingNats()
    client = Client(nats, principal=Principal(TENANT, ACCOUNT), tenancy=Mode.SHARED)

    asyncio.run(client.query(LIST))

    assert nats.sent == [f"oe.v1.{ACCOUNT}.ge.query.graphs.list"]


def test_a_dedicated_mode_client_keeps_the_plain_subject() -> None:
    nats = RecordingNats()
    for client in (Client(nats), Client(nats, principal=Principal(TENANT)), Client(nats, principal=Principal(TENANT, ACCOUNT))):
        asyncio.run(client.query(LIST))

    assert nats.sent == [LIST] * 3


def test_a_shared_mode_client_needs_a_principal_with_an_account() -> None:
    with pytest.raises(ValueError):
        Client(RecordingNats(), tenancy=Mode.SHARED)
    with pytest.raises(ValueError):
        Client(RecordingNats(), principal=Principal(TENANT), tenancy=Mode.SHARED)


def test_subject_helpers_put_the_account_in_the_third_token() -> None:
    assert shared_subject(LIST, ACCOUNT) == f"oe.v1.{ACCOUNT}.ge.query.graphs.list"
    assert subject_template(LIST) == "oe.v1.{account}.ge.query.graphs.list"
    with pytest.raises(ValueError):
        shared_subject("other.subject", ACCOUNT)


@pytest.fixture
def spans(monkeypatch: pytest.MonkeyPatch) -> InMemorySpanExporter:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr("openengine.telemetry.propagation.trace.get_tracer", provider.get_tracer)
    return exporter


def test_span_names_use_the_subject_template_and_never_the_account_key(spans: InMemorySpanExporter) -> None:
    server, _ = _server(Shared(InMemoryAccounts({ACCOUNT: TENANT})))
    nats = RecordingNats()
    client = Client(nats, principal=Principal(TENANT, ACCOUNT), tenancy=Mode.SHARED)

    _send(server, f"oe.v1.{ACCOUNT}.ge.query.graphs.list")
    asyncio.run(client.query(LIST))

    names = sorted(span.name for span in spans.get_finished_spans())
    assert names == ["process oe.v1.{account}.ge.query.graphs.list", "send oe.v1.{account}.ge.query.graphs.list"]
    assert all(ACCOUNT not in name for name in names)
    # The destination attribute still carries the real subject, where it belongs.
    assert {span.attributes["messaging.destination.name"] for span in spans.get_finished_spans()} == {nats.sent[0]}


def test_dedicated_span_names_are_the_plain_subject(spans: InMemorySpanExporter) -> None:
    server, _ = _server()
    _send(server, LIST)
    asyncio.run(Client(RecordingNats()).query(LIST))

    assert sorted(span.name for span in spans.get_finished_spans()) == [f"process {LIST}", f"send {LIST}"]
