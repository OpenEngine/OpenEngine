"""Durable workspace acceptance, read models and restartable allocation.

The managed provider must make provision idempotent by runtime_id. A crash
between a provider effect and its database commit repeats that identity, never
an unkeyed create. Prototype WorkspaceService adapters are deliberately separate.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from openengine.messaging import ErrorCode, Request, Route, ServiceError, envelopes
from openengine.messaging.errors import invalid_argument

from virtual_control_plane.application.ports import AllocationProvider
from virtual_control_plane.application.transactions import CommandIdConflict, UnitOfWork

SCHEMAS = "virtual_control_plane/v1/schemas"
ALLOCATION_TIMEOUT_SECONDS = 60
APPLY = f"{SCHEMAS}/commands.schema.json#/$defs/apply_workspace_command"


def now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def identity(prefix: str) -> str:
    return prefix + uuid4().hex


class Lifecycle:
    def __init__(self, store: UnitOfWork, providers: Mapping[str, AllocationProvider]) -> None:
        self.store, self.providers = store, providers
        self._worker: asyncio.Task | None = None
        self._allocation_lock = asyncio.Lock()

    def commands(self) -> dict[str, Route]:
        return {"workspaces.apply": Route(self.apply, receipts=True)}

    def queries(self) -> dict[str, Route]:
        return {
            key: Route(handler, f"{SCHEMAS}/queries.schema.json#/$defs/{schema}")
            for key, handler, schema in [
                ("capabilities.get", self.capabilities, "get_capabilities_query"),
                ("workspaces.list", self.list, "list_workspaces_query"),
                ("workspaces.{workspace_id}.get", self.get, "get_workspace_query"),
                ("operations.{operation_id}.get", self.operation, "get_operation_query"),
            ]
        }

    async def capabilities(self, request: Request) -> dict[str, Any]:
        selected = request.body.get("params", {}).get("provider")
        return {"providers": [p.capabilities for name, p in sorted(self.providers.items())
                              if selected is None or name == selected]}

    async def apply(self, request: Request) -> dict[str, Any]:
        # Server authorizes before invoking this receipt-aware handler.
        body = request.body
        try:
            async with self.store.transaction() as tx:
                if original := await tx.replay(request.subject, body):
                    return original
                try:
                    response = await self._accept(tx, body)
                except ServiceError as error:
                    response = {"command_id": body["command_id"], "status": "rejected", "error": error.to_dict()}
                return await tx.record_command(request.subject, body, response)
        except CommandIdConflict:
            raise ServiceError(ErrorCode.COMMAND_ID_CONFLICT, "command ID has different immutable inputs",
                               details={"command_id": body["command_id"], "original_subject": request.subject}) from None

    def _validate(self, body: dict[str, Any]) -> None:
        envelopes.check(envelopes.COMMAND, body)
        envelopes.check(APPLY, body)
        if len(body["causal_path"]) >= envelopes.MAX_CAUSAL_PATH:
            raise ServiceError(ErrorCode.CAUSAL_PATH_TOO_LONG, "no room for lifecycle event",
                               details={"max_length": envelopes.MAX_CAUSAL_PATH, "length": len(body["causal_path"]) + 1})
        if body.get("deadline_at") and datetime.fromisoformat(body["deadline_at"]) <= datetime.now(UTC):
            raise ServiceError(ErrorCode.DEADLINE_EXCEEDED, "command expired before acceptance",
                               details={"deadline_at": body["deadline_at"], "received_at": now()})

    async def _accept(self, tx, body: dict[str, Any]) -> dict[str, Any]:
        self._validate(body)
        document = body["payload"]["workspace"]
        spec, name = document["spec"], document["metadata"]["name"]
        existing = await tx.workspace_named(name)
        expected = body.get("expected_version")
        if expected is not None and existing is None:
            raise ServiceError(ErrorCode.NOT_FOUND, "workspace does not exist",
                               details={"resource_type": "workspace", "resource_id": name})
        if expected is not None and expected != existing["snapshot"]["version"]:
            raise ServiceError(ErrorCode.VERSION_CONFLICT, "workspace version changed",
                               details={"expected_version": expected, "actual_version": existing["snapshot"]["version"]})
        provider = self.providers.get(spec["provider"])
        if provider is None:
            raise ServiceError(ErrorCode.UNSUPPORTED_CAPABILITY, "provider is not configured",
                               details={"capability": "provider." + spec["provider"]})
        caps = provider.capabilities
        supported = {key for key in ("checkpoint", "suspend") if caps[key]}
        supported.add("isolation_" + caps["isolation"])
        if caps["restore"] != "none":
            supported.add("restore_" + caps["restore"])
        if set(spec.get("requiredCapabilities", [])) - supported or set(spec.get("tools", [])) - set(caps["tools"]):
            raise ServiceError(ErrorCode.UNSUPPORTED_CAPABILITY, "provider lacks requested capability or tool",
                               details={"capability": "workspaces.apply"})
        for resource, maximum in caps["max_resources"].items():
            source = {"cpu_millis": "cpuMillis", "memory_mib": "memoryMib", "disk_gib": "diskGib"}[resource]
            if spec.get("resources", {}).get(source, 0) > maximum:
                raise invalid_argument([("payload.workspace.spec.resources." + source, "exceeds provider maximum")])
        if existing:
            previous = existing["specification"]["spec"]
            if any(previous.get(key, {} if key == "resources" else []) != spec.get(key, {} if key == "resources" else [])
                   for key in ("provider", "resources", "requiredCapabilities")):
                raise invalid_argument([("payload.workspace.spec", "provider, resources and requiredCapabilities are immutable")])
            if existing["specification"] != document:
                raise ServiceError(ErrorCode.UNSUPPORTED_CAPABILITY, "workspace updates are not supported yet",
                                   details={"capability": "workspaces.apply.update"})
            return {"command_id": body["command_id"], "status": "accepted",
                    "operation_id": existing["operation_id"], "resource_id": existing["snapshot"]["workspace_id"]}
        workspace_id, operation_id, runtime_id = identity("ws_"), identity("op_"), identity("rt_")
        timestamp = now()
        snapshot = {
            "workspace_id": workspace_id, "name": name, "version": 1, "provider": spec["provider"],
            "state": "provisioning", "resources": {
                target: spec["resources"][source] for source, target in
                [("cpuMillis", "cpu_millis"), ("memoryMib", "memory_mib"), ("diskGib", "disk_gib")]
                if source in spec.get("resources", {})
            }, "tools": spec.get("tools", []), "required_capabilities": spec.get("requiredCapabilities", []),
            "labels": {target: spec["owner"][source] for source, target in
                       [("jobId", "job_id"), ("executionId", "execution_id"), ("stepAttemptId", "step_attempt_id")]
                       if source in spec.get("owner", {})},
            "active_attempt_ids": [], "created_at": timestamp, "updated_at": timestamp,
        }
        operation = {"operation_id": operation_id, "command_id": body["command_id"], "operation": "workspaces.apply",
                     "status": "pending", "resources": {"workspace_id": workspace_id},
                     "created_at": timestamp, "updated_at": timestamp}
        await tx.put("workspaces", workspace_id, {"snapshot": snapshot, "specification": document,
                                                  "operation_id": operation_id})
        await tx.put("operations", operation_id, {"snapshot": operation, "causal_path": body["causal_path"]})
        await tx.put("workspace_runtimes", workspace_id, {"runtime_id": runtime_id, "generation": 1,
                                                        "provider": spec["provider"], "locator": None})
        await self._event(tx, snapshot, operation, body["causal_path"], "provisioning", {"provider": spec["provider"]})
        return {"command_id": body["command_id"], "status": "accepted", "operation_id": operation_id,
                "resource_id": workspace_id}

    async def _event(self, tx, workspace, operation, path, state, payload) -> None:
        event_id = identity("evt_")
        await tx.append_event({
            "event_id": event_id, "event_type": "workspace." + state, "occurred_at": now(), "producer": "vcp",
            "causal_path": [*path, event_id], "resources": {"workspace_id": workspace["workspace_id"],
                **({"runtime_id": workspace["runtime"]["runtime_id"]} if "runtime" in workspace else {})},
            "resource_version": workspace["version"], "payload": {
                "operation": {key: operation[key] for key in ("operation_id", "command_id")}, **payload,
            },
        })

    async def _read(self, collection, key):
        async with self.store.transaction() as tx:
            value = await tx.get(collection, key)
        if value is None or "snapshot" not in value:
            raise ServiceError(ErrorCode.NOT_FOUND, "resource does not exist",
                               details={"resource_type": collection, "resource_id": key})
        return value["snapshot"]

    async def get(self, request: Request) -> dict[str, Any]:
        return {"workspace": await self._read("workspaces", request.parameters["workspace_id"])}

    async def operation(self, request: Request) -> dict[str, Any]:
        return {"operation": await self._read("operations", request.parameters["operation_id"])}

    async def list(self, request: Request) -> dict[str, Any]:
        params = request.body.get("params", {})
        filters = {key: (sorted(value) if isinstance(value, list) else value)
                   for key, value in params.items() if key not in ("cursor", "limit")}
        after = None
        if cursor := params.get("cursor"):
            try:
                decoded = json.loads(base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True))
                after = decoded["after"]
                if decoded["version"] != 1 or not isinstance(after, list) or len(after) != 2 or not all(isinstance(v, str) for v in after):
                    raise ValueError("malformed")  # noqa: TRY301 -- normalize all cursor errors below
                if decoded["filters"] != filters:
                    raise ValueError("filters_changed")  # noqa: TRY301 -- normalize all cursor errors below
            except (binascii.Error, ValueError, KeyError, TypeError) as error:
                raise ServiceError(ErrorCode.INVALID_CURSOR, "invalid workspace cursor",
                                   details={"reason": "filters_changed" if str(error) == "filters_changed" else "malformed"}) from None
        limit = int(params.get("limit", 50))
        items = await self.store.workspace_page(filters, after, limit + 1)
        def key(w):
            return [w["created_at"], w["workspace_id"]]
        page = items[:limit]
        cursor = None
        if len(items) > limit:
            cursor = base64.urlsafe_b64encode(json.dumps({"version": 1, "filters": filters, "after": key(page[-1])}).encode()).decode().rstrip("=")
        return {"items": page, "next_cursor": cursor}

    async def reconcile(self) -> None:
        async with self._allocation_lock:
            await self._reconcile()

    async def _reconcile(self) -> None:
        """One pass. Only one worker owns this state directory, enforced by service startup."""
        # The service's exclusive state-directory ownership and reconciliation
        # lock claim each page in memory. Keep pending durable until completion:
        # cancellation releases claims and restart retries the same runtime IDs.
        after = ""
        tasks = set()
        exhausted = False
        try:
            while tasks or not exhausted:
                if not exhausted and len(tasks) < 8:
                    async with self.store.transaction() as tx:
                        pending = await tx.pending_allocations(8 - len(tasks), after)
                    exhausted = not pending
                    for record in pending:
                        tasks.add(asyncio.create_task(self._allocate(record)))
                        after = record["snapshot"]["operation_id"]
                if tasks:
                    done, tasks = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        task.result()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _allocate(self, record) -> None:
        operation = record["snapshot"]
        workspace_id = operation["resources"]["workspace_id"]
        async with self.store.transaction() as tx:
            workspace = await tx.get("workspaces", workspace_id)
            runtime = await tx.get("workspace_runtimes", workspace_id)
        provider = self.providers.get(runtime["provider"])
        if provider is None:
            return  # Retain ownership and wait for the owning provider to return.
        try:
            async with asyncio.timeout(ALLOCATION_TIMEOUT_SECONDS):
                locator = await provider.provision(runtime["runtime_id"], workspace["specification"])
            error = None
        except TimeoutError:
            return  # Leave durable pending state for an idempotent retry.
        except ServiceError as failure:
            if failure.retryable:
                return
            error = failure.to_dict()
        except Exception:
            logging.getLogger(__name__).exception("provider allocation failed")
            error = ServiceError(ErrorCode.PROVIDER_ERROR, "provider allocation failed").to_dict()
        async with self.store.transaction() as tx:
            snapshot = workspace["snapshot"]
            snapshot.update(state="failed" if error else "ready", version=snapshot["version"] + 1, updated_at=now())
            operation.update(status="failed" if error else "succeeded", updated_at=now())
            if error:
                snapshot["failure"] = operation["error"] = error
                payload = {"error": error, "runtime_generation": 1}
            else:
                runtime["locator"] = locator
                snapshot["runtime"] = {"runtime_id": runtime["runtime_id"], "generation": 1,
                                       "state": "running", "started_at": now()}
                operation["result"] = {"workspace_id": workspace_id, "created": True,
                                       "runtime_id": runtime["runtime_id"], "runtime_generation": 1}
                payload = {"runtime_generation": 1}
            await tx.put("workspaces", workspace_id, workspace)
            await tx.put("operations", operation["operation_id"], record)
            await tx.put("workspace_runtimes", workspace_id, runtime)
            await self._event(tx, snapshot, operation, record["causal_path"], snapshot["state"], payload)

    def start(self) -> None:
        self._worker = asyncio.create_task(self._run())

    async def _run(self) -> None:
        while True:
            try:
                await self.reconcile()
            except Exception:
                logging.getLogger(__name__).exception("workspace reconciliation failed; retrying")
            await asyncio.sleep(1)

    async def close(self) -> None:
        if self._worker:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
