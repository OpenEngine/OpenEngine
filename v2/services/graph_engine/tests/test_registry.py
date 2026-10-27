"""graphs.apply and operations.{operation_id}.get through the server adapter, request bytes in, reply bytes out."""

from __future__ import annotations

import asyncio
import copy
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from openengine.contracts.validate import load_contract, validate_message
from openengine.messaging import Server
from openengine.persistence import DatabaseConfig

from graph_engine.persistence import PERSISTENCE
from graph_engine import registry
from graph_engine.registry import Registry

APPLY = "oe.v1.ge.control.graphs.apply"
EXAMPLE = Path(__file__).parents[3] / "packages/contracts/graph_engine/v1/resources/examples/implement-review.graph.yaml"
GRAPH = yaml.safe_load(EXAMPLE.read_text())


@pytest.fixture
def send(tmp_path: Path):
    config = DatabaseConfig(sqlite_directory=tmp_path)
    PERSISTENCE.upgrade(config)
    store = PERSISTENCE.open(config)
    registry = Registry(store)
    server = Server(None, "ge", "nats://127.0.0.1:4222", queries=registry.queries(), commands=registry.commands())
    loop = asyncio.new_event_loop()

    def send(subject: str, body: dict[str, Any]) -> dict[str, Any]:
        return json.loads(loop.run_until_complete(server.handle(subject, json.dumps(body).encode())))

    yield send
    loop.run_until_complete(store.close())
    loop.close()


def _apply(command_id: str, payload: dict[str, Any], **envelope: Any) -> dict[str, Any]:
    return {"command_id": command_id, "causal_path": [command_id], "payload": payload, **envelope}


def _operation(send, reply: dict[str, Any]) -> dict[str, Any]:
    subject = f"oe.v1.ge.query.operations.{reply['operation_id']}.get"
    response = send(subject, {"params": {}})
    assert validate_message(load_contract("graph_engine"), "GetOperationResponse", response, in_reply_to=subject) == []
    return response["result"]["operation"]


def _changed(description: str) -> dict[str, Any]:
    graph = copy.deepcopy(GRAPH)
    graph["spec"]["description"] = description
    return graph


def test_apply_creates_then_keeps_or_adds_revisions_under_one_graph_id(send) -> None:
    created = send(APPLY, _apply("cmd_1", {"graph": GRAPH}))
    assert validate_message(load_contract("graph_engine"), "ApplyGraphResponse", created, in_reply_to=APPLY) == []
    # The same graph with its keys in another order is the same definition.
    reordered = send(APPLY, _apply("cmd_2", {"graph": dict(reversed(GRAPH.items()))}))
    updated = send(APPLY, _apply("cmd_3", {"graph": _changed("Changed.")}, expected_version=1))

    graph_id = created["resource_id"]
    assert graph_id.startswith("graph_") and reordered["resource_id"] == updated["resource_id"] == graph_id
    assert [_operation(send, reply)["result"] for reply in (created, reordered, updated)] == [
        {"graph": {"graph_id": graph_id, "revision": 1}, "created": True},
        {"graph": {"graph_id": graph_id, "revision": 1}, "created": False},
        {"graph": {"graph_id": graph_id, "revision": 2}, "created": True},
    ]
    operation = _operation(send, created)
    assert operation | {"created_at": "", "updated_at": ""} == {
        "operation_id": created["operation_id"], "command_id": "cmd_1", "operation": "graphs.apply",
        "status": "succeeded", "resources": {"graph_id": graph_id}, "created_at": "", "updated_at": "",
        "result": {"graph": {"graph_id": graph_id, "revision": 1}, "created": True},
    }


def test_a_retried_command_gets_its_original_reply_and_a_reused_id_conflicts(send) -> None:
    first = send(APPLY, _apply("cmd_1", {"graph": GRAPH}))

    assert send(APPLY, _apply("cmd_1", {"graph": GRAPH})) == first
    conflict = send(APPLY, _apply("cmd_1", {"graph": _changed("Changed.")}))
    assert conflict["status"] == "rejected"
    assert conflict["error"]["code"] == "COMMAND_ID_CONFLICT"
    assert conflict["error"]["details"] == {
        "command_id": "cmd_1", "original_subject": APPLY, "original_operation_id": first["operation_id"],
    }
    # So does the same payload with another expected_version or causal path.
    assert send(APPLY, _apply("cmd_1", {"graph": GRAPH}, expected_version=1))["error"]["code"] == "COMMAND_ID_CONFLICT"
    assert send(APPLY, {**_apply("cmd_1", {"graph": GRAPH}), "causal_path": ["cmd_0", "cmd_1"]})["error"]["code"] == "COMMAND_ID_CONFLICT"


def test_expected_version_mismatch_is_rejected_and_replayed(send) -> None:
    missing = send(APPLY, _apply("cmd_1", {"graph": GRAPH}, expected_version=1))
    assert missing["error"]["code"] == "NOT_FOUND"

    send(APPLY, _apply("cmd_2", {"graph": GRAPH}))
    stale = send(APPLY, _apply("cmd_3", {"graph": _changed("Changed.")}, expected_version=2))
    assert stale["error"]["code"] == "VERSION_CONFLICT"
    assert stale["error"]["details"] == {"expected_version": 2, "actual_version": 1}
    assert validate_message(load_contract("graph_engine"), "ApplyGraphResponse", stale, in_reply_to=APPLY) == []

    send(APPLY, _apply("cmd_4", {"graph": _changed("Changed.")}))
    # Revision 2 now exists, but a retry still gets the rejection it was given.
    assert send(APPLY, _apply("cmd_3", {"graph": _changed("Changed.")}, expected_version=2)) == stale


def test_python_graphs_are_unsupported(send) -> None:
    command = _apply("cmd_1", {"python": {"name": "review", "source": "workflow = None"}})

    reply = send(APPLY, command)

    assert reply["error"]["code"] == "UNSUPPORTED_CAPABILITY"
    assert reply["error"]["details"] == {"capability": "graphs.apply.python"}
    assert send(APPLY, command) == reply


def test_a_graph_breaking_its_rules_is_invalid_argument(send) -> None:
    graph = copy.deepcopy(GRAPH)
    graph["spec"]["flow"][0]["route"][1]["to"] = "nowhere"

    reply = send(APPLY, _apply("cmd_1", {"graph": graph}))

    assert reply["error"]["code"] == "INVALID_ARGUMENT"
    assert any(violation["field"].startswith("payload.graph.spec.flow") for violation in reply["error"]["details"]["violations"])


def test_operation_queries_name_a_known_well_formed_id(send) -> None:
    unknown = send("oe.v1.ge.query.operations.op_unknown.get", {"params": {}})
    assert unknown["error"]["code"] == "NOT_FOUND"
    assert unknown["error"]["details"] == {"resource_type": "operation", "resource_id": "op_unknown"}

    malformed = send("oe.v1.ge.query.operations.cmd_1.get", {"params": {}})
    assert malformed["error"]["code"] == "INVALID_ARGUMENT"
    assert [violation["field"] for violation in malformed["error"]["details"]["violations"]] == ["subject.operation_id"]


def test_a_command_breaking_its_schema_is_rejected_durably(send) -> None:
    broken = send(APPLY, _apply("cmd_1", {"graph": {}}))
    assert broken["error"]["code"] == "INVALID_ARGUMENT"
    assert validate_message(load_contract("graph_engine"), "ApplyGraphResponse", broken, in_reply_to=APPLY) == []

    # Its ID is spent: a valid graph under it conflicts rather than being accepted.
    assert send(APPLY, _apply("cmd_1", {"graph": GRAPH}))["error"]["code"] == "COMMAND_ID_CONFLICT"
    assert send(APPLY, _apply("cmd_1", {"graph": {}})) == broken
    # And a broken command reusing an accepted command's ID conflicts too.
    accepted = send(APPLY, _apply("cmd_2", {"graph": GRAPH}))
    conflict = send(APPLY, _apply("cmd_2", {"graph": {}}))
    assert conflict["error"]["code"] == "COMMAND_ID_CONFLICT"
    assert conflict["error"]["details"]["original_operation_id"] == accepted["operation_id"]


def test_a_passed_deadline_refuses_acceptance_but_not_a_replay(send) -> None:
    late = send(APPLY, _apply("cmd_1", {"graph": GRAPH}, deadline_at="2000-01-01T00:00:00Z"))
    assert late["error"]["code"] == "DEADLINE_EXCEEDED"
    assert late["error"]["details"]["deadline_at"] == "2000-01-01T00:00:00Z"
    assert validate_message(load_contract("graph_engine"), "ApplyGraphResponse", late, in_reply_to=APPLY) == []
    assert send(APPLY, _apply("cmd_1", {"graph": GRAPH}, deadline_at="2000-01-01T00:00:00Z")) == late

    # Nothing was recorded, so a later apply creates revision 1.
    created = send(APPLY, _apply("cmd_2", {"graph": GRAPH}, deadline_at="2999-01-01T00:00:00Z"))
    assert created["status"] == "accepted"
    assert _operation(send, created)["result"]["created"] is True


def test_an_accepted_command_is_replayed_after_its_deadline(send, monkeypatch) -> None:
    command = _apply("cmd_1", {"graph": GRAPH}, deadline_at="2030-01-01T00:00:00Z")
    accepted = send(APPLY, command)

    monkeypatch.setattr(registry, "datetime", _Later)

    assert send(APPLY, command) == accepted


class _Later(datetime):
    @classmethod
    def now(cls, tz=None):  # noqa: ANN001, ANN206
        return datetime(2031, 1, 1, tzinfo=tz)
