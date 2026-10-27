"""graphs.apply, graphs.list, graphs.{graph_id}.get and operations.{operation_id}.get through the server adapter, request bytes in, reply bytes out."""

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

from graph_engine.persistence import PERSISTENCE, sqlite
from graph_engine.registry import Registry

APPLY = "oe.v1.ge.control.graphs.apply"
LIST = "oe.v1.ge.query.graphs.list"
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


def test_a_command_breaking_its_envelope_is_rejected_durably(send) -> None:
    broken = send(APPLY, {"command_id": "cmd_1", "causal_path": [], "payload": []})
    assert broken["error"]["code"] == "INVALID_ARGUMENT"
    assert validate_message(load_contract("graph_engine"), "ApplyGraphResponse", broken, in_reply_to=APPLY) == []
    assert send(APPLY, _apply("cmd_1", {"graph": GRAPH}))["error"]["code"] == "COMMAND_ID_CONFLICT"
    # Without a well-formed command ID there is nothing to keep a receipt under.
    unidentified = send(APPLY, {"command_id": "not a token", "payload": []})
    assert unidentified["error"]["code"] == "INVALID_ARGUMENT"


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

    monkeypatch.setattr(sqlite, "datetime", _Later)

    assert send(APPLY, command) == accepted


class _Later(datetime):
    @classmethod
    def now(cls, tz=None):  # noqa: ANN001, ANN206
        return datetime(2031, 1, 1, tzinfo=tz)


def _named(name: str, description: str | None = None) -> dict[str, Any]:
    graph = _changed(description or GRAPH["spec"]["description"])
    graph["metadata"]["name"] = name
    return graph


def _listed(send, params: dict[str, Any]) -> dict[str, Any]:
    reply = send(LIST, {"params": params})
    assert validate_message(load_contract("graph_engine"), "ListGraphsResponse", reply, in_reply_to=LIST) == []
    return reply


def test_graphs_list_pages_by_name_with_a_cursor_bound_to_its_prefix(send) -> None:
    for name in ("triage", "review-b", "review-a", "deploy", "review-c"):
        send(APPLY, _apply(f"cmd_{name}", {"graph": _named(name)}))
    send(APPLY, _apply("cmd_changed", {"graph": _named("review-a", "Changed.")}))

    names, cursor, pages = [], None, 0
    while True:
        result = _listed(send, {"name_prefix": "review", "limit": 2, "cursor": cursor})["result"]
        names += [graph["name"] for graph in result["items"]]
        pages += 1
        if (cursor := result["next_cursor"]) is None:
            break
    assert (names, pages) == (["review-a", "review-b", "review-c"], 2)

    everything = _listed(send, {})["result"]
    assert [graph["name"] for graph in everything["items"]] == ["deploy", "review-a", "review-b", "review-c", "triage"]
    assert everything["next_cursor"] is None
    assert everything["items"][1]["latest_revision"] == 2
    assert everything["items"][1]["description"] == "Changed."

    issued = _listed(send, {"name_prefix": "review", "limit": 1})["result"]["next_cursor"]
    changed = send(LIST, {"params": {"limit": 1, "cursor": issued}})
    assert changed["error"]["code"] == "INVALID_CURSOR"
    assert changed["error"]["details"] == {"reason": "filters_changed"}
    for malformed in ("not-a-cursor", "e30", "W10"):
        assert send(LIST, {"params": {"cursor": malformed}})["error"]["details"] == {"reason": "malformed"}


def _get(send, graph_id: str, params: dict[str, Any]) -> dict[str, Any]:
    subject = f"oe.v1.ge.query.graphs.{graph_id}.get"
    reply = send(subject, {"params": params})
    assert validate_message(load_contract("graph_engine"), "GetGraphResponse", reply, in_reply_to=subject) == []
    return reply


def test_graphs_get_returns_the_latest_or_a_named_revision(send) -> None:
    graph_id = send(APPLY, _apply("cmd_1", {"graph": GRAPH}))["resource_id"]
    send(APPLY, _apply("cmd_2", {"graph": _changed("Changed.")}))

    latest = _get(send, graph_id, {})["result"]
    assert latest["graph"]["latest_revision"] == latest["revision"]["revision"] == 2
    assert json.loads(latest["revision"]["source"])["spec"]["description"] == "Changed."
    first = _get(send, graph_id, {"revision": 1})["result"]
    assert json.loads(first["revision"]["source"]) == GRAPH
    assert first["revision"]["source_sha256"] != latest["revision"]["source_sha256"]
    assert first["revision"]["format"] == "resource"
    assert first["revision"]["inputs"] == {
        "instruction": {"required": True, "description": "The change to make"},
        "base": {"description": "The branch to build on; empty means main"},
        "runner": {},
    }
    assert first["revision"]["nodes"] == ["implement", "publish", "ci", "reviewers", "consolidate", "approve"]
    assert "source" not in _get(send, graph_id, {"include_source": False})["result"]["revision"]


def test_graphs_get_of_an_unknown_graph_or_revision_is_not_found(send) -> None:
    graph_id = send(APPLY, _apply("cmd_1", {"graph": GRAPH}))["resource_id"]

    for subject_id, params in (("graph_missing", {}), (graph_id, {"revision": 9})):
        reply = _get(send, subject_id, params)
        assert reply["error"]["code"] == "NOT_FOUND"
        assert reply["error"]["details"] == {"resource_type": "graph", "resource_id": subject_id}
    assert send("oe.v1.ge.query.graphs.op_1.get", {"params": {}})["error"]["code"] == "INVALID_ARGUMENT"
