"""Transport-free validation complements the real broker authority tests."""

import asyncio

import pytest

from openengine.messaging import CallerIngress, Principal, Server, ServiceError, caller_subject

A = Principal("alice")
INGRESS = CallerIngress(frozenset({A}))
SUBJECT = "oe.v1.caller.alice.ge.query.resources.get"


@pytest.mark.parametrize("subject,reply", [
    ("oe.v1.ge.query.resources.get", "_INBOX.oe.alice.x"),
    (SUBJECT.replace("alice", "bob"), "_INBOX.oe.bob.x"),
    (SUBJECT, "_INBOX.oe.bob.x"),
    (SUBJECT, "_INBOX.oe.alice"),
    (SUBJECT, "_INBOX.oe.alice.>"),
    (SUBJECT, "_INBOX.oe.alice.x..y"),
    (SUBJECT, ""),
    (SUBJECT.replace(".ge.", ".vcp."), "_INBOX.oe.alice.x"),
    (SUBJECT.replace(".query.", ".event."), "_INBOX.oe.alice.x"),
])
def test_invalid_ingress_is_denied(subject, reply):
    with pytest.raises(ServiceError, match="PERMISSION_DENIED"):
        INGRESS.verify("ge", subject, reply)


def test_verified_identity_preserves_logical_route():
    assert INGRESS.verify("ge", SUBJECT, "_INBOX.oe.alice.random.token") == (A, "oe.v1.ge.query.resources.get")
    assert caller_subject(A, "oe.v1.ge.query.resources.get") == SUBJECT


@pytest.mark.parametrize("principal", ["a.b", "*", ">", "", "a b", "a\n"])
def test_principal_cannot_inject_subject_tokens(principal):
    with pytest.raises(ValueError):
        Principal(principal)


@pytest.mark.parametrize("subject", ["oe.v1.ge.event.changed", "oe.v1.ge.query.>", "oe.v1.ge.query..get", "other"])
def test_client_rejects_non_request_routes(subject):
    with pytest.raises(ValueError):
        caller_subject(A, subject)


def test_caller_ingress_requires_resource_authorizer():
    server = Server(None, "ge", "nats://127.0.0.1:4222", caller_ingress=INGRESS)
    with pytest.raises(ValueError, match="explicit resource authorizer"):
        asyncio.run(server.start())


def test_transport_free_handle_cannot_admit_missing_identity():
    import json

    server = Server(None, "ge", "nats://127.0.0.1:4222", caller_ingress=INGRESS)
    response = asyncio.run(server.handle("oe.v1.ge.query.resources.get", b'{"params":{}}'))
    assert json.loads(response)["error"]["code"] == "PERMISSION_DENIED"
