from __future__ import annotations

import copy
import json
import shutil
import subprocess
from typing import Any

import pytest
import reference
import validate
import yaml
from validate import CONTRACTS, SERVICES, load_contract, validate_message

STREAMS = CONTRACTS.parent.parent / "infra" / "nats" / "streams"

# The subjects the V2 work order lists, per service. Events are checked separately.
REQUEST_SUBJECTS = {
    "connection_bus": {
        "oe.v1.cxn.control.connections.apply",
        "oe.v1.cxn.control.connections.{connection_id}.disable",
        "oe.v1.cxn.control.connections.{connection_id}.delete",
        "oe.v1.cxn.query.connections.list",
        "oe.v1.cxn.query.connections.{connection_id}.get",
    },
    "graph_engine": {
        "oe.v1.ge.control.graphs.apply",
        "oe.v1.ge.control.graphs.{graph_id}.run",
        "oe.v1.ge.control.jobs.submit",
        "oe.v1.ge.control.jobs.{job_id}.steer",
        "oe.v1.ge.control.jobs.{job_id}.pause",
        "oe.v1.ge.control.jobs.{job_id}.resume",
        "oe.v1.ge.control.jobs.{job_id}.cancel",
        "oe.v1.ge.control.approvals.{approval_id}.decide",
        "oe.v1.ge.control.loops.apply",
        "oe.v1.ge.control.loops.{loop_id}.pause",
        "oe.v1.ge.control.loops.{loop_id}.resume",
        "oe.v1.ge.control.loops.{loop_id}.remove",
        "oe.v1.ge.query.graphs.list",
        "oe.v1.ge.query.graphs.{graph_id}.get",
        "oe.v1.ge.query.jobs.list",
        "oe.v1.ge.query.jobs.{job_id}.get",
        "oe.v1.ge.query.jobs.{job_id}.executions.list",
        "oe.v1.ge.query.jobs.{job_id}.workspaces.list",
        "oe.v1.ge.query.jobs.{job_id}.approvals.list",
        "oe.v1.ge.query.executions.{execution_id}.get",
        "oe.v1.ge.query.executions.{execution_id}.steps.list",
        "oe.v1.ge.query.loops.list",
        "oe.v1.ge.query.loops.{loop_id}.get",
        "oe.v1.ge.query.operations.{operation_id}.get",
    },
    "tool_service": {
        "oe.v1.tools.apply",
        "oe.v1.tools.get",
        "oe.v1.tools.list",
        "oe.v1.tools.delete",
        "oe.v1.tools.invoke.{namespace}.{name}",
        "oe.v1.toolgrants.apply",
        "oe.v1.toolgrants.get",
        "oe.v1.toolgrants.list",
        "oe.v1.toolgrants.delete",
        "oe.v1.toolinvocations.get",
        "oe.v1.toolinvocations.list",
        "oe.v1.toolinvocations.cancel",
    },
    "virtual_control_plane": {
        "oe.v1.vcp.control.workspaces.apply",
        "oe.v1.vcp.control.workspaces.{workspace_id}.ensure",
        "oe.v1.vcp.control.workspaces.{workspace_id}.execute",
        "oe.v1.vcp.control.workspaces.{workspace_id}.steer",
        "oe.v1.vcp.control.workspaces.{workspace_id}.cancel",
        "oe.v1.vcp.control.workspaces.{workspace_id}.checkpoint",
        "oe.v1.vcp.control.workspaces.{workspace_id}.suspend",
        "oe.v1.vcp.control.workspaces.{workspace_id}.destroy",
        "oe.v1.vcp.control.approvals.{approval_id}.decide",
        "oe.v1.vcp.query.capabilities.get",
        "oe.v1.vcp.query.workspaces.list",
        "oe.v1.vcp.query.workspaces.{workspace_id}.get",
        "oe.v1.vcp.query.workspaces.{workspace_id}.sessions.list",
        "oe.v1.vcp.query.workspaces.{workspace_id}.approvals.list",
        "oe.v1.vcp.query.workspaces.{workspace_id}.artifacts.list",
        "oe.v1.vcp.query.sessions.{session_id}.get",
        "oe.v1.vcp.query.sessions.{session_id}.events.list",
        "oe.v1.vcp.query.operations.{operation_id}.get",
    },
}
SHORT = {"connection_bus": "cxn", "graph_engine": "ge", "tool_service": "tools", "virtual_control_plane": "vcp"}
# Where each service's subjects live: `oe.v1.{short}.` unless listed here.
NAMESPACES = {
    "tool_service": ("oe.v1.tools.", "oe.v1.toolgrants.", "oe.v1.toolinvocations.", "oe.v1.events.toolinvocations."),
}


def namespaces(service: str) -> tuple[str, ...]:
    return NAMESPACES.get(service, (f"oe.v1.{SHORT[service]}.",))


def is_event(address: str) -> bool:
    """Event subjects: `oe.v1.{short}.events.…`, or `oe.v1.events.…` for the tool service."""
    tokens = address.split(".")
    return tokens[2] == "events" or tokens[3] == "events"


def addresses(service: str) -> set[str]:
    channels = load_contract(service).document["channels"].values()
    return {channel["address"] for channel in channels if channel["address"]}


def nats_matches(filter_: str, subject: str) -> bool:
    pattern, tokens = filter_.split("."), subject.split(".")
    for index, part in enumerate(pattern):
        if part == ">":
            return len(tokens) > index
        if index >= len(tokens) or (part != "*" and part != tokens[index]):
            return False
    return len(pattern) == len(tokens)


def example(service: str, name: str) -> dict[str, Any]:
    return json.loads((CONTRACTS / service / "v1" / "examples" / name).read_text())


def errors_for(service: str, case: dict[str, Any]) -> list[str]:
    return validate_message(
        load_contract(service),
        case["message"],
        case["payload"],
        subject=case.get("subject"),
        in_reply_to=case.get("in_reply_to"),
        headers=case.get("headers"),
    )


@pytest.mark.parametrize("service", SERVICES)
def test_every_message_has_valid_examples(service: str) -> None:
    assert validate.check(load_contract(service)) == []


def test_finds_every_service_contract() -> None:
    assert set(SERVICES) <= set(REQUEST_SUBJECTS)
    assert "connection_bus" in SERVICES


@pytest.mark.parametrize("service", SERVICES)
def test_defines_exactly_the_listed_request_subjects(service: str) -> None:
    assert {address for address in addresses(service) if not is_event(address)} == REQUEST_SUBJECTS[service]


@pytest.mark.parametrize("service", SERVICES)
def test_every_request_operation_replies_with_its_own_response(service: str) -> None:
    contract = load_contract(service)
    for name, operation in contract.document["operations"].items():
        if operation["action"] == "receive":
            assert len(operation["reply"]["messages"]) == 1, name


@pytest.mark.parametrize("service", SERVICES)
def test_events_land_in_their_stream_and_requests_in_none(service: str) -> None:
    streams = {path.stem: json.loads(path.read_text())["subjects"] for path in STREAMS.glob("*.json")}
    for address in addresses(service):
        assert address.startswith(namespaces(service)), address
        concrete = ".".join("x" if token.startswith("{") else token for token in address.split("."))
        captured = [name for name, filters in streams.items() if any(nats_matches(f, concrete) for f in filters)]
        if is_event(address):
            assert captured == [f"{SHORT[service]}-events"], address
        else:
            assert captured == [], f"{address} is request/reply but a stream captures it"


def test_no_streams_or_consumers_are_added() -> None:
    assert sorted(path.name for path in STREAMS.glob("*.json")) == [
        "cxn-egress.json", "cxn-events.json", "cxn-ingress.json", "ge-events.json", "tools-events.json", "vcp-events.json",
    ]
    consumers = STREAMS.parent / "consumers"
    assert [path.name for path in consumers.iterdir()] == ["README.md"]


@pytest.mark.parametrize("service", SERVICES)
def test_checked_in_reference_is_current(service: str) -> None:
    path = reference.reference_path(service)
    assert path.exists() and path.read_text() == reference.page(service), (
        f"{path.relative_to(CONTRACTS)} is stale: run `uv run python packages/contracts/tooling/reference.py`"
    )


def test_pages_builds_on_contract_changes() -> None:
    root = CONTRACTS.parents[2]
    workflow = yaml.safe_load((root / ".github/workflows/pages.yml").read_text())
    # PyYAML's YAML 1.1 loader interprets the bare `on` key as True.
    triggers = workflow[True]
    for event in ("push", "pull_request"):
        assert "v2/packages/contracts/**" in triggers[event]["paths"]


@pytest.mark.parametrize("service", reference.NAMES)
def test_published_contract_links(service: str) -> None:
    url = f"https://openengine.cc/contracts/{service}/v1/"
    for path in (CONTRACTS.parents[1] / "ARCHITECTURE.md", CONTRACTS / "README.md"):
        assert f"]({url})" in path.read_text(), path


# Malformed variants of valid CXN examples. Each must be rejected.

COMMAND = example("connection_bus", "connections.disable.command.json")
RESPONSE = example("connection_bus", "connections.apply.response.accepted.json")
EVENT = example("connection_bus", "connection.applied.event.re-enabled.json")
QUERY_RESPONSE = example("connection_bus", "connections.get.response.json")


def mutated(base: dict[str, Any], change) -> dict[str, Any]:
    case = copy.deepcopy(base)
    change(case)
    return case


def set_path(*keys_and_value):
    *keys, value = keys_and_value

    def change(case):
        node = case
        for key in keys[:-1]:
            node = node[key]
        node[keys[-1]] = value

    return change


def drop(*keys):
    def change(case):
        node = case
        for key in keys[:-1]:
            node = node[key]
        del node[keys[-1]]

    return change


MALFORMED = {
    "command without command_id": (COMMAND, drop("payload", "command_id")),
    "command without causal_path": (COMMAND, drop("payload", "causal_path")),
    "command without payload": (COMMAND, drop("payload", "payload")),
    "causal path not ending in command_id": (COMMAND, set_path("payload", "causal_path", ["cmd_other"])),
    "empty causal path": (COMMAND, set_path("payload", "causal_path", [])),
    "causal path with a repeat": (COMMAND, set_path("payload", "causal_path", ["cmd_01JAX8DIS", "cmd_01JAX8DIS"])),
    "causal path over 32 elements": (
        COMMAND,
        set_path("payload", "causal_path", [f"cmd_{n}" for n in range(32)] + ["cmd_01JAX8DIS"]),
    ),
    "causal path element with a dot": (COMMAND, set_path("payload", "causal_path", ["gh.123", "cmd_01JAX8DIS"])),
    "unknown envelope field": (COMMAND, set_path("payload", "replyTo", "_INBOX.x")),
    "actor in the payload": (COMMAND, set_path("payload", "payload", "actor", "alice")),
    "deadline not in UTC": (COMMAND, set_path("payload", "deadline_at", "2026-10-19T23:30:00+02:00")),
    "expected_version zero": (COMMAND, set_path("payload", "expected_version", 0)),
    "unknown disable field": (COMMAND, set_path("payload", "payload", "enabled", True)),
    "subject token with a dot": (COMMAND, set_path("subject", "oe.v1.cxn.control.connections.conn_a.b.disable")),
    "subject token wildcard": (COMMAND, set_path("subject", "oe.v1.cxn.control.connections.*.disable")),
    "subject token full wildcard": (COMMAND, set_path("subject", "oe.v1.cxn.control.connections.>.disable")),
    "subject token of the wrong type": (COMMAND, set_path("subject", "oe.v1.cxn.control.connections.job_1.disable")),
    "subject on another service": (COMMAND, set_path("subject", "oe.v1.ge.control.connections.conn_a.disable")),
    "accepted without operation_id": (RESPONSE, drop("payload", "operation_id")),
    "accepted with an error": (
        RESPONSE,
        set_path("payload", "error", {"code": "INTERNAL", "message": "x", "retryable": True}),
    ),
    "rejected without an error": (RESPONSE, set_path("payload", "status", "rejected")),
    "unknown status": (RESPONSE, set_path("payload", "status", "done")),
    "response to another operation": (RESPONSE, set_path("in_reply_to", "oe.v1.cxn.control.connections.conn_a.delete")),
    "response with a bad request subject token": (
        example("connection_bus", "connections.delete.response.invalid-state.json"),
        set_path("in_reply_to", "oe.v1.cxn.control.connections.bad*.delete"),
    ),
    "invalid argument marked retryable": (
        QUERY_RESPONSE,
        set_path("payload", {"status": "error", "error": {"code": "INVALID_ARGUMENT", "message": "x", "retryable": True}}),
    ),
    "query ok with an error": (
        QUERY_RESPONSE,
        set_path("payload", "error", {"code": "INTERNAL", "message": "x", "retryable": True}),
    ),
    "version conflict without details": (
        example("connection_bus", "connections.apply.response.version-conflict.json"),
        drop("payload", "error", "details"),
    ),
    "event subject names another connection": (
        EVENT,
        set_path("subject", "oe.v1.cxn.events.connections.conn_other.connection.applied"),
    ),
    "event subject names another type": (
        EVENT,
        set_path("subject", "oe.v1.cxn.events.connections.conn_acme_gh.connection.disabled"),
    ),
    "event path not ending in event_id": (EVENT, set_path("payload", "causal_path", ["cmd_01JAX9CFG"])),
    "event without Nats-Msg-Id": (EVENT, drop("headers", "Nats-Msg-Id")),
    "event Nats-Msg-Id differs from event_id": (EVENT, set_path("headers", "Nats-Msg-Id", "evt_other")),
    "event traceparent in upper case": (
        EVENT,
        set_path("headers", "traceparent", "00-4BF92F3577B34DA6A3CE929D0E0E4736-B7AD6B7169203331-01"),
    ),
    "command traceparent without a parent span": (
        COMMAND,
        set_path("headers", "traceparent", "00-4bf92f3577b34da6a3ce929d0e0e4736-01"),
    ),
    "event of the wrong type for its message": (EVENT, set_path("payload", "event_type", "connection.disabled")),
    "event from another producer": (EVENT, set_path("payload", "producer", "ge")),
    "event without resources": (EVENT, drop("payload", "resources")),
    "event missing its resource version": (EVENT, drop("payload", "resource_version")),
    "event timestamp without zone": (EVENT, set_path("payload", "occurred_at", "2026-10-19T22:10:40")),
    "secret in a connection snapshot": (
        QUERY_RESPONSE,
        set_path("payload", "result", "connection", "credential", "ghp_secret"),
    ),
}


@pytest.mark.parametrize("variant", MALFORMED)
def test_rejects_malformed_variants(variant: str) -> None:
    base, change = MALFORMED[variant]
    assert errors_for("connection_bus", mutated(base, change)) != []


def test_the_unmutated_bases_are_valid() -> None:
    for base in (COMMAND, RESPONSE, EVENT, QUERY_RESPONSE):
        assert errors_for("connection_bus", base) == []


def test_a_causal_path_of_exactly_32_elements_is_allowed() -> None:
    case = mutated(COMMAND, set_path("payload", "causal_path", [f"cmd_{n}" for n in range(31)] + ["cmd_01JAX8DIS"]))
    assert errors_for("connection_bus", case) == []


@pytest.mark.integration
@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_asyncapi_parser_accepts_every_document() -> None:
    tooling = CONTRACTS / "tooling"
    if not (tooling / "node_modules").exists():
        pytest.skip("run `npm ci` in packages/contracts/tooling first")
    result = subprocess.run(["node", "validate.mjs"], cwd=tooling, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_bundled_documents_resolve_every_file_reference() -> None:
    for service in SERVICES:
        text = json.dumps(reference.self_contained(service))
        assert ".schema.json" not in text and "/examples/" not in text, service
