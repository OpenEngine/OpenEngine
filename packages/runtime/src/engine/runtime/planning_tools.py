"""Milestone planning operations and their MCP bridge."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import asdict, dataclass, replace
import json
import secrets
import sys
from uuid import uuid4

from engine.domain import (
    AgentInstance,
    Milestone,
    MilestoneId,
    Project,
    ProjectId,
    project_id_for_instance,
)
from engine.ports import McpServerConfig, StateStore

McpRequestId = str | int
_PROTOCOL_VERSION = "2025-06-18"
_SERVER_NAME = "planning"
PLANNING_TOOL_NAMES = (
    "add_milestone",
    "list_milestones",
    "update_milestone",
    "delete_milestone",
)


async def project_chat_capabilities(
    store: StateStore, instance: AgentInstance
) -> tuple[str, ...]:
    """Grant milestone tools only when this conversation owns a project."""
    project = await store.load_project(project_id_for_instance(instance.instance_id))
    return PLANNING_TOOL_NAMES if project is not None else ()


@dataclass(frozen=True, slots=True)
class ProjectPlan:
    """One project and its milestones, shaped for tool responses."""

    project: Project
    milestones: tuple[Milestone, ...]

    def render(self) -> str:
        """Present the plan compactly while keeping every usable identifier."""
        lines = [f"# {self.project.name}", f"Project ID: `{self.project.project_id}`"]
        if not self.milestones:
            return "\n".join((*lines, "", "No milestones yet."))
        lines.extend(("", "## Milestones"))
        for milestone in self.milestones:
            lines.extend(("", f"- **{milestone.name}** (`{milestone.milestone_id}`)"))
            if milestone.description:
                lines.append(f"  {milestone.description}")
            dependencies = ", ".join(
                f"`{dependency}`" for dependency in milestone.dependencies
            )
            lines.append(f"  Depends on: {dependencies or 'nothing'}")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, object]:
        return {
            "project": _project_dict(self.project),
            "milestones": [_milestone_dict(item) for item in self.milestones],
        }


class PlanningTools:
    """Validated milestone mutations over the state-store boundary."""

    def __init__(self, store: StateStore) -> None:
        self._store = store

    async def add_milestone(
        self,
        project_id: ProjectId,
        name: str,
        description: str,
        dependencies: Sequence[MilestoneId] = (),
    ) -> Milestone:
        project = await self._require_project(project_id)
        normalized = _unique_dependencies(dependencies)
        await self._validate_dependencies(project.project_id, normalized)
        milestone = Milestone(
            milestone_id=MilestoneId(f"milestone-{uuid4().hex[:12]}"),
            project_id=project.project_id,
            name=_non_empty(name, "name"),
            description=_non_empty(description, "description"),
            dependencies=normalized,
        )
        await self._store.save_milestone(milestone)
        return milestone

    async def list_milestones(self, project_id: ProjectId) -> ProjectPlan:
        project = await self._require_project(project_id)
        milestones = tuple(await self._store.list_milestones(project.project_id))
        return ProjectPlan(project, _dependency_order(milestones))

    async def update_milestone(
        self,
        milestone_id: MilestoneId,
        *,
        name: str | None = None,
        description: str | None = None,
        dependencies: Sequence[MilestoneId] | None = None,
    ) -> Milestone:
        milestone = await self._require_milestone(milestone_id)
        if name is None and description is None and dependencies is None:
            raise ValueError("update_milestone requires at least one changed field")
        normalized = (
            milestone.dependencies
            if dependencies is None
            else _unique_dependencies(dependencies)
        )
        await self._validate_dependencies(
            milestone.project_id, normalized, milestone_id=milestone.milestone_id
        )
        updated = replace(
            milestone,
            name=milestone.name if name is None else _non_empty(name, "name"),
            description=(
                milestone.description
                if description is None
                else _non_empty(description, "description")
            ),
            dependencies=normalized,
        )
        await self._reject_cycle(updated)
        await self._store.save_milestone(updated)
        return updated

    async def delete_milestone(self, milestone_id: MilestoneId) -> Milestone:
        milestone = await self._require_milestone(milestone_id)
        dependents = [
            item.milestone_id
            for item in await self._store.list_milestones(milestone.project_id)
            if milestone.milestone_id in item.dependencies
        ]
        if dependents:
            raise ValueError(
                f"milestone {milestone_id!r} is required by {', '.join(dependents)}"
            )
        await self._store.delete_milestone(milestone.milestone_id)
        return milestone

    async def _require_project(self, project_id: ProjectId) -> Project:
        project = await self._store.load_project(project_id)
        if project is None:
            raise KeyError(f"no project {project_id!r}")
        return project

    async def _require_milestone(self, milestone_id: MilestoneId) -> Milestone:
        milestone = await self._store.load_milestone(milestone_id)
        if milestone is None:
            raise KeyError(f"no milestone {milestone_id!r}")
        return milestone

    async def _validate_dependencies(
        self,
        project_id: ProjectId,
        dependencies: Sequence[MilestoneId],
        milestone_id: MilestoneId | None = None,
    ) -> None:
        if milestone_id is not None and milestone_id in dependencies:
            raise ValueError("a milestone cannot depend on itself")
        for dependency_id in dependencies:
            dependency = await self._store.load_milestone(dependency_id)
            if dependency is None:
                raise ValueError(f"no dependency milestone {dependency_id!r}")
            if dependency.project_id != project_id:
                raise ValueError(
                    f"dependency {dependency_id!r} belongs to another project"
                )

    async def _reject_cycle(self, updated: Milestone) -> None:
        milestones = {
            item.milestone_id: item
            for item in await self._store.list_milestones(updated.project_id)
        }
        milestones[updated.milestone_id] = updated

        def reaches_updated(milestone_id: MilestoneId, seen: set[MilestoneId]) -> bool:
            if milestone_id == updated.milestone_id:
                return True
            if milestone_id in seen:
                return False
            seen.add(milestone_id)
            milestone = milestones.get(milestone_id)
            return milestone is not None and any(
                reaches_updated(dependency, seen)
                for dependency in milestone.dependencies
            )

        if any(reaches_updated(dependency, set()) for dependency in updated.dependencies):
            raise ValueError("milestone dependencies cannot contain a cycle")


class PlanningMcpBroker:
    """Expose one process-local planning tool set to a provider CLI."""

    def __init__(
        self,
        store: StateStore,
        capabilities: Sequence[str],
        instance: AgentInstance,
    ) -> None:
        self._store = store
        self._tools = PlanningTools(store)
        self._capabilities = _validated_capabilities(capabilities)
        self._project_id = project_id_for_instance(instance.instance_id)
        self._token = secrets.token_hex(32)
        self._server: asyncio.Server | None = None

    async def __aenter__(self) -> PlanningMcpBroker:
        self._server = await asyncio.start_server(
            self._handle_connection, "127.0.0.1", 0
        )
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    @property
    def config(self) -> McpServerConfig:
        if self._server is None or not self._server.sockets:
            raise RuntimeError("planning MCP broker has not been started")
        port = self._server.sockets[0].getsockname()[1]
        return McpServerConfig(
            name=_SERVER_NAME,
            command=sys.executable,
            args=(
                "-m",
                "engine.runtime.planning_mcp_server",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--token",
                self._token,
                *(
                    argument
                    for capability in self._capabilities
                    for argument in ("--capability", capability)
                ),
            ),
        )

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            request = json.loads(await reader.readline())
            response = await self._submit(request)
        except Exception as error:
            response = {"ok": False, "error": f"invalid planning request: {error}"}
        writer.write(json.dumps(response, separators=(",", ":")).encode() + b"\n")
        with suppress(ConnectionError):
            await writer.drain()
        writer.close()
        with suppress(ConnectionError):
            await writer.wait_closed()

    async def _submit(self, request: object) -> dict[str, object]:
        if not isinstance(request, dict) or request.get("token") != self._token:
            return {"ok": False, "error": "invalid planning credential"}
        name = request.get("name")
        arguments = request.get("arguments")
        if name not in self._capabilities:
            return {"ok": False, "error": f"planning tool is not granted: {name}"}
        if not isinstance(arguments, dict):
            return {"ok": False, "error": f"{name} arguments must be an object"}
        try:
            text, data = await self._call(name, arguments)
        except (KeyError, TypeError, ValueError) as error:
            return {"ok": False, "error": str(error)}
        return {"ok": True, "text": text, "data": data}

    async def _call(
        self, name: object, arguments: dict[str, object]
    ) -> tuple[str, dict[str, object]]:
        if name == "add_milestone":
            _exact_arguments(
                arguments,
                required={"name", "description"},
                optional={"dependencies"},
            )
            milestone = await self._tools.add_milestone(
                self._project_id,
                _string(arguments, "name"),
                _string(arguments, "description"),
                _dependency_arguments(arguments),
            )
            return f"Added milestone `{milestone.milestone_id}`.", _milestone_dict(
                milestone
            )
        if name == "list_milestones":
            _exact_arguments(arguments, required=set())
            plan = await self._tools.list_milestones(self._project_id)
            return plan.render(), plan.to_dict()
        if name == "update_milestone":
            _exact_arguments(
                arguments,
                required={"milestone_id"},
                optional={"name", "description", "dependencies"},
            )
            milestone_id = await self._require_owned_milestone(arguments)
            milestone = await self._tools.update_milestone(
                milestone_id,
                name=_optional_string(arguments, "name"),
                description=_optional_string(arguments, "description"),
                dependencies=(
                    _dependency_arguments(arguments)
                    if "dependencies" in arguments
                    else None
                ),
            )
            return f"Updated milestone `{milestone.milestone_id}`.", _milestone_dict(
                milestone
            )
        if name == "delete_milestone":
            _exact_arguments(arguments, required={"milestone_id"})
            milestone_id = await self._require_owned_milestone(arguments)
            milestone = await self._tools.delete_milestone(milestone_id)
            return f"Deleted milestone `{milestone.milestone_id}`.", _milestone_dict(
                milestone
            )
        raise ValueError(f"unknown planning tool: {name}")

    async def _require_owned_milestone(
        self, arguments: dict[str, object]
    ) -> MilestoneId:
        milestone_id = MilestoneId(_string(arguments, "milestone_id"))
        milestone = await self._store.load_milestone(milestone_id)
        if milestone is None:
            raise KeyError(f"no milestone {milestone_id!r}")
        if milestone.project_id != self._project_id:
            raise ValueError(f"milestone {milestone_id!r} belongs to another project")
        return milestone_id


def _tool_specs(capabilities: Sequence[str]) -> list[dict[str, object]]:
    identifier = {"type": "string", "minLength": 1}
    dependencies = {"type": "array", "items": identifier, "default": []}
    specs = [
        {
            "name": "add_milestone",
            "description": (
                "Add a milestone to the project owned by this planning conversation."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": identifier,
                    "description": identifier,
                    "dependencies": dependencies,
                },
                "required": ["name", "description"],
                "additionalProperties": False,
            },
        },
        {
            "name": "list_milestones",
            "description": (
                "Read the plan for the project owned by this planning conversation."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
        {
            "name": "update_milestone",
            "description": "Update a milestone's planning details.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "milestone_id": identifier,
                    "name": identifier,
                    "description": identifier,
                    "dependencies": dependencies,
                },
                "required": ["milestone_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "delete_milestone",
            "description": "Delete an unused milestone from a project plan.",
            "inputSchema": {
                "type": "object",
                "properties": {"milestone_id": identifier},
                "required": ["milestone_id"],
                "additionalProperties": False,
            },
        },
    ]
    granted = set(capabilities)
    return [spec for spec in specs if spec["name"] in granted]


async def _forward_call(
    host: str,
    port: int,
    token: str,
    request_id: McpRequestId,
    name: object,
    arguments: object,
) -> dict[str, object]:
    reader, writer = await asyncio.open_connection(host, port)
    writer.write(
        json.dumps(
            {
                "token": token,
                "request_id": request_id,
                "name": name,
                "arguments": arguments,
            },
            separators=(",", ":"),
        ).encode()
        + b"\n"
    )
    await writer.drain()
    response = json.loads(await reader.readline())
    writer.close()
    with suppress(ConnectionError):
        await writer.wait_closed()
    return response


async def _mcp_response(
    host: str,
    port: int,
    token: str,
    capabilities: Sequence[str],
    request: object,
) -> dict[str, object] | None:
    if not isinstance(request, dict):
        return _rpc_error(None, -32600, "Invalid Request")
    request_id = request.get("id")
    method = request.get("method")
    if isinstance(method, str) and method.startswith("notifications/"):
        return None
    if method == "initialize":
        params = request.get("params")
        protocol = (
            params.get("protocolVersion", _PROTOCOL_VERSION)
            if isinstance(params, dict)
            else _PROTOCOL_VERSION
        )
        return _rpc_result(
            request_id,
            {
                "protocolVersion": protocol,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "engine-planning", "version": "1"},
            },
        )
    if method == "ping":
        return _rpc_result(request_id, {})
    if method == "tools/list":
        return _rpc_result(request_id, {"tools": _tool_specs(capabilities)})
    if method != "tools/call":
        return _rpc_error(request_id, -32601, "Method not found")
    if not isinstance(request_id, (str, int)) or isinstance(request_id, bool):
        return _rpc_error(request_id, -32600, "Tool calls require a request id")
    params = request.get("params")
    if not isinstance(params, dict):
        return _rpc_error(request_id, -32602, "Invalid tool parameters")
    forwarded = await _forward_call(
        host,
        port,
        token,
        request_id,
        params.get("name"),
        params.get("arguments", {}),
    )
    if forwarded.get("ok") is not True:
        return _rpc_result(
            request_id,
            {
                "content": [{"type": "text", "text": str(forwarded.get("error"))}],
                "isError": True,
            },
        )
    return _rpc_result(
        request_id,
        {
            "content": [{"type": "text", "text": str(forwarded["text"])}],
            "structuredContent": forwarded["data"],
        },
    )


async def _serve_stdio(
    host: str, port: int, token: str, capabilities: Sequence[str]
) -> None:
    while line := await asyncio.to_thread(sys.stdin.buffer.readline):
        try:
            response = await _mcp_response(
                host, port, token, capabilities, json.loads(line)
            )
            if response is None:
                continue
        except Exception as error:
            response = _rpc_error(None, -32700, f"Parse error: {error}")
        sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
        sys.stdout.flush()


def _rpc_result(request_id: object, result: object) -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _rpc_error(request_id: object, code: int, message: str) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def _non_empty(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _string(arguments: dict[str, object], name: str) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _optional_string(arguments: dict[str, object], name: str) -> str | None:
    return _string(arguments, name) if name in arguments else None


def _dependency_arguments(arguments: dict[str, object]) -> tuple[MilestoneId, ...]:
    value = arguments.get("dependencies", [])
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise ValueError("dependencies must be an array of milestone ids")
    return tuple(MilestoneId(item.strip()) for item in value)


def _exact_arguments(
    arguments: dict[str, object],
    *,
    required: set[str],
    optional: set[str] | None = None,
) -> None:
    optional = optional or set()
    missing = required - set(arguments)
    unexpected = set(arguments) - required - optional
    if missing:
        raise ValueError(f"missing arguments: {', '.join(sorted(missing))}")
    if unexpected:
        raise ValueError(f"unexpected arguments: {', '.join(sorted(unexpected))}")


def _unique_dependencies(
    dependencies: Sequence[MilestoneId],
) -> tuple[MilestoneId, ...]:
    return tuple(dict.fromkeys(dependencies))


def _validated_capabilities(capabilities: Sequence[str]) -> tuple[str, ...]:
    missing = [name for name in capabilities if name not in PLANNING_TOOL_NAMES]
    if missing:
        raise ValueError(f"unknown planning capabilities: {missing}")
    granted = set(capabilities)
    return tuple(name for name in PLANNING_TOOL_NAMES if name in granted)


def _dependency_order(milestones: Sequence[Milestone]) -> tuple[Milestone, ...]:
    by_id = {item.milestone_id: item for item in milestones}
    ordered: list[Milestone] = []
    visited: set[MilestoneId] = set()

    def visit(milestone: Milestone) -> None:
        if milestone.milestone_id in visited:
            return
        visited.add(milestone.milestone_id)
        for dependency in milestone.dependencies:
            if dependency in by_id:
                visit(by_id[dependency])
        ordered.append(milestone)

    for item in milestones:
        visit(item)
    return tuple(ordered)


def _project_dict(project: Project) -> dict[str, object]:
    return {"project_id": project.project_id, "name": project.name}


def _milestone_dict(milestone: Milestone) -> dict[str, object]:
    value = asdict(milestone)
    value["dependencies"] = list(milestone.dependencies)
    return value


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--token", required=True)
    parser.add_argument("--capability", action="append", default=[])
    arguments = parser.parse_args()
    asyncio.run(
        _serve_stdio(
            arguments.host,
            arguments.port,
            arguments.token,
            _validated_capabilities(arguments.capability),
        )
    )


__all__ = [
    "PLANNING_TOOL_NAMES",
    "PlanningMcpBroker",
    "PlanningTools",
    "ProjectPlan",
    "project_chat_capabilities",
]
