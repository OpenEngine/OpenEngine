"""The graph registry's subjects: `graphs.apply`, and `operations.{operation_id}.get` for what it recorded.

A Graph resource is kept as the canonical JSON of what was applied (sorted keys,
no spacing), so reformatting a file or reordering its keys changes nothing,
and its SHA-256 decides whether an apply is unchanged. Python graphs are
refused with UNSUPPORTED_CAPABILITY until GE can load them.

Every command gets a receipt: its acceptance, recorded in the same transaction
as the revision and operation, or its rejection, unless the rejection was
retryable. A retry with the same command ID and the same subject, payload,
`expected_version` and causal path gets that reply again; anything else with
that ID is COMMAND_ID_CONFLICT. `expected_version` is the graph's latest
revision, so naming it for a graph that does not exist yet is NOT_FOUND.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from openengine.contracts.resources import validate_resource
from openengine.messaging import ErrorCode, Request, Route, ServiceError
from openengine.messaging.errors import invalid_argument

from graph_engine.persistence.store import Command, CommandIdConflict, Definition, Graphs, Operation, VersionConflict

SCHEMAS = "graph_engine/v1/schemas"


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


class Registry:
    def __init__(self, store: Graphs) -> None:
        self.store = store

    def commands(self) -> dict[str, Route]:
        return {"graphs.apply": Route(self.apply, f"{SCHEMAS}/commands.schema.json#/$defs/apply_graph_command")}

    def queries(self) -> dict[str, Route]:
        return {
            "operations.{operation_id}.get": Route(self.operation, f"{SCHEMAS}/queries.schema.json#/$defs/get_operation_query"),
        }

    async def apply(self, request: Request) -> dict[str, Any]:
        command = _command(request)
        payload = request.body["payload"]
        try:
            response = await self._apply(command, payload, request.body.get("expected_version"))
        except CommandIdConflict as conflict:
            details = {"command_id": conflict.command_id, "original_subject": conflict.subject}
            if conflict.operation_id:
                details["original_operation_id"] = conflict.operation_id
            raise ServiceError(ErrorCode.COMMAND_ID_CONFLICT, str(conflict), details=details) from None
        if response["status"] == "rejected":
            raise ServiceError.from_dict(response["error"])
        return response

    async def _apply(self, command: Command, payload: dict[str, Any], expected_version: int | None) -> dict[str, Any]:
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
        try:
            return await self.store.apply(command, definition, expected_version)
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

    async def operation(self, request: Request) -> dict[str, Any]:
        operation_id = request.parameters["operation_id"]
        if (operation := await self.store.operation(operation_id)) is None:
            raise ServiceError(
                ErrorCode.NOT_FOUND, f"no operation {operation_id}",
                details={"resource_type": "operation", "resource_id": operation_id},
            )
        return {"operation": _snapshot(operation)}
