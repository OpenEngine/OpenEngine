"""The graph registry's subjects: `graphs.apply`, `graphs.list`, `graphs.{graph_id}.get`, and `operations.{operation_id}.get`.

A Graph resource is kept as the canonical JSON of what was applied (sorted keys,
no spacing), so reformatting a file or reordering its keys changes nothing,
and its SHA-256 decides whether an apply is unchanged. Python graphs are
refused with UNSUPPORTED_CAPABILITY until GE can load them.

Every command gets a receipt: its acceptance, recorded in the same transaction
as the revision and operation, or its rejection, unless the rejection was
retryable. That includes a command breaking its schema, so the handler checks
the command envelope and schema rather than the server, and one whose `deadline_at`
has passed by the time the store would accept it, which is DEADLINE_EXCEEDED. A retry with the same command ID and the same subject, payload,
`expected_version` and causal path gets that reply again; anything else with
that ID is COMMAND_ID_CONFLICT. `expected_version` is the graph's latest
revision, so naming it for a graph that does not exist yet is NOT_FOUND.
A Graph whose source, JSON-encoded as `graphs.{graph_id}.get` replies with it,
would pass MAX_SOURCE_BYTES is INVALID_ARGUMENT, so every revision GE keeps
can be read back within NATS's default 1 MiB payload.

`graphs.list` pages by key on (name, graph_id). Its cursor is the last
graph's key and the `name_prefix` it was issued for, as base64url JSON, so it
never expires, and reusing it with another prefix is INVALID_CURSOR.
A revision's `inputs` and `nodes` are read from its source when it is asked
for: the inputs it declares, and its step names in stage order.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from datetime import datetime
from typing import Any

from openengine.contracts.graphs import STAGES
from openengine.contracts.resources import validate_resource
from openengine.messaging import ErrorCode, Request, Route, ServiceError, envelopes
from openengine.messaging.errors import invalid_argument

from graph_engine.persistence.store import (
    Command,
    CommandIdConflict,
    DeadlineExceeded,
    Definition,
    Graph,
    GraphRevision,
    Graphs,
    Operation,
    VersionConflict,
)

SCHEMAS = "graph_engine/v1/schemas"
APPLY = f"{SCHEMAS}/commands.schema.json#/$defs/apply_graph_command"
DEFAULT_LIMIT = 50
# Half of NATS's default max_payload, leaving the rest of a get reply room.
MAX_SOURCE_BYTES = 512 * 1024


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _command(request: Request) -> Command:
    body = request.body
    repeated = {key: body.get(key) for key in ("payload", "expected_version", "causal_path")}
    fingerprint = hashlib.sha256(canonical({"subject": request.subject, **repeated}).encode()).hexdigest()
    return Command(body["command_id"], request.subject, fingerprint)


def _timestamp(when: datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _snapshot(operation: Operation) -> dict[str, Any]:
    snapshot = {
        "operation_id": operation.operation_id,
        "command_id": operation.command_id,
        "operation": operation.operation,
        "status": operation.status,
        "resources": operation.resources,
        "created_at": _timestamp(operation.created_at),
        "updated_at": _timestamp(operation.updated_at),
    }
    return snapshot | {key: value for key in ("result", "error") if (value := getattr(operation, key)) is not None}


def _graph(graph: Graph) -> dict[str, Any]:
    snapshot = {
        "graph_id": graph.graph_id,
        "name": graph.name,
        "latest_revision": graph.latest_revision,
        "created_at": _timestamp(graph.created_at),
        "updated_at": _timestamp(graph.updated_at),
    }
    return snapshot | ({"description": graph.description} if graph.description is not None else {})


def _revision(revision: GraphRevision, include_source: bool) -> dict[str, Any]:
    spec = json.loads(revision.source)["spec"] if revision.format == "resource" else {}
    inputs = {
        declared["name"]: {key: declared[key] for key in ("required", "description") if key in declared}
        for declared in spec.get("inputs", [])
    }
    snapshot = {
        "graph_id": revision.graph_id,
        "revision": revision.revision,
        "format": revision.format,
        "source_sha256": revision.source_sha256,
        "inputs": inputs,
        "nodes": [step["name"] for stage in STAGES for step in spec.get(stage, [])],
        "created_at": _timestamp(revision.created_at),
    }
    return snapshot | ({"source": revision.source} if include_source else {})


def _cursor(graph: Graph, name_prefix: str | None) -> str:
    key = canonical({"after": [graph.name, graph.graph_id], "name_prefix": name_prefix})
    return base64.urlsafe_b64encode(key.encode()).decode().rstrip("=")


def _after(cursor: str, name_prefix: str | None) -> tuple[str, str]:
    malformed = ServiceError(ErrorCode.INVALID_CURSOR, "the cursor is not one graphs.list issued",
                             details={"reason": "malformed"})
    try:
        key = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        name, graph_id = key["after"]
        issued_for = key["name_prefix"]
    except (binascii.Error, ValueError, KeyError, TypeError):
        raise malformed from None
    if not (isinstance(name, str) and isinstance(graph_id, str)):
        raise malformed
    if issued_for != name_prefix:
        raise ServiceError(ErrorCode.INVALID_CURSOR, "the cursor was issued for another name_prefix",
                           details={"reason": "filters_changed"})
    return name, graph_id


def _not_found(graph_id: str, message: str) -> ServiceError:
    return ServiceError(ErrorCode.NOT_FOUND, message, details={"resource_type": "graph", "resource_id": graph_id})


class Registry:
    def __init__(self, store: Graphs) -> None:
        self.store = store

    def commands(self) -> dict[str, Route]:
        return {"graphs.apply": Route(self.apply, receipts=True)}

    def queries(self) -> dict[str, Route]:
        return {
            "graphs.list": Route(self.list, f"{SCHEMAS}/queries.schema.json#/$defs/list_graphs_query"),
            "graphs.{graph_id}.get": Route(self.get, f"{SCHEMAS}/queries.schema.json#/$defs/get_graph_query"),
            "operations.{operation_id}.get": Route(self.operation, f"{SCHEMAS}/queries.schema.json#/$defs/get_operation_query"),
        }

    async def apply(self, request: Request) -> dict[str, Any]:
        command = _command(request)
        try:
            response = await self._apply(command, request.body)
        except CommandIdConflict as conflict:
            details = {"command_id": conflict.command_id, "original_subject": conflict.subject}
            if conflict.operation_id:
                details["original_operation_id"] = conflict.operation_id
            raise ServiceError(ErrorCode.COMMAND_ID_CONFLICT, str(conflict), details=details) from None
        if response["status"] == "rejected":
            raise ServiceError.from_dict(response["error"])
        return response

    async def _apply(self, command: Command, body: dict[str, Any]) -> dict[str, Any]:
        try:
            envelopes.check(envelopes.COMMAND, body)
            envelopes.check(APPLY, body)
        except ServiceError as error:
            return await self._reject(command, error)
        payload, expected_version = body["payload"], body.get("expected_version")
        if "python" in payload:
            return await self._reject(command, ServiceError(
                ErrorCode.UNSUPPORTED_CAPABILITY, "GE does not load Python graphs yet; apply a Graph resource",
                details={"capability": "graphs.apply.python"},
            ))
        graph = payload["graph"]
        # The command's schema has checked the Graph's structure; this adds the rules that span its fields.
        if problems := validate_resource(graph, "Graph"):
            return await self._reject(command, invalid_argument(
                [(f"payload.graph.{problem.path}", problem.message) for problem in problems]
            ))
        definition = Definition(graph["metadata"]["name"], graph["spec"].get("description"), "resource", canonical(graph))
        if (size := len(json.dumps(definition.source, ensure_ascii=False).encode())) > MAX_SOURCE_BYTES:
            return await self._reject(command, invalid_argument(
                [("payload.graph", f"its source is {size} bytes as a reply carries it; the most is {MAX_SOURCE_BYTES}")]
            ))
        deadline = datetime.fromisoformat(body["deadline_at"]) if "deadline_at" in body else None
        try:
            # A retry of a command accepted in time is replayed by its receipt, not refused for its deadline.
            return await self.store.apply(command, definition, expected_version, deadline)
        except DeadlineExceeded as late:
            return await self._reject(command, ServiceError(
                ErrorCode.DEADLINE_EXCEEDED, str(late),
                details={"deadline_at": body["deadline_at"], "received_at": _timestamp(late.received)},
            ))
        except VersionConflict as conflict:
            if conflict.actual is None:
                error = ServiceError(
                    ErrorCode.NOT_FOUND, f"no graph named {definition.name} to be at revision {conflict.expected}",
                    details={"resource_type": "graph", "resource_id": definition.name},
                )
            else:
                error = ServiceError(
                    ErrorCode.VERSION_CONFLICT,
                    f"graph {definition.name} is at revision {conflict.actual}, not {conflict.expected}",
                    details={"expected_version": conflict.expected, "actual_version": conflict.actual},
                )
            return await self._reject(command, error)

    async def _reject(self, command: Command, error: ServiceError) -> dict[str, Any]:
        return await self.store.reject(
            command, {"command_id": command.command_id, "status": "rejected", "error": error.to_dict()}
        )

    async def list(self, request: Request) -> dict[str, Any]:
        params = request.body["params"]
        name_prefix, limit = params.get("name_prefix"), int(params.get("limit", DEFAULT_LIMIT))
        after = _after(cursor, name_prefix) if (cursor := params.get("cursor")) is not None else None
        # One more than the page shows whether there is a next page.
        found = await self.store.graphs(limit + 1, name_prefix, after)
        page = found[:limit]
        return {
            "items": [_graph(graph) for graph in page],
            "next_cursor": _cursor(page[-1], name_prefix) if len(found) > limit else None,
        }

    async def get(self, request: Request) -> dict[str, Any]:
        graph_id, params = request.parameters["graph_id"], request.body["params"]
        if (graph := await self.store.graph(graph_id)) is None:
            raise _not_found(graph_id, f"no graph {graph_id}")
        number = params.get("revision", graph.latest_revision)
        if (revision := await self.store.revision(graph_id, number)) is None:
            raise _not_found(graph_id, f"graph {graph.name} has no revision {number}; its latest is {graph.latest_revision}")
        return {"graph": _graph(graph), "revision": _revision(revision, params.get("include_source", True))}

    async def operation(self, request: Request) -> dict[str, Any]:
        operation_id = request.parameters["operation_id"]
        if (operation := await self.store.operation(operation_id)) is None:
            raise ServiceError(
                ErrorCode.NOT_FOUND, f"no operation {operation_id}",
                details={"resource_type": "operation", "resource_id": operation_id},
            )
        return {"operation": _snapshot(operation)}
