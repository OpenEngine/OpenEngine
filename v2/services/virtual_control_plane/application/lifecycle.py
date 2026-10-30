"""Durable workspace acceptance, read models and restartable lifecycle operations.

The managed provider must make provision and ensure idempotent by the persisted
ownership (workspace, runtime, generation). A crash between a provider effect
and its database commit repeats that identity, never an unkeyed create. One
worker claims accepted operations as `running`, runs the provider outside any
transaction, retries transient failures with capped backoff and commits the
terminal state with its public event. Prototype WorkspaceService adapters are
deliberately separate.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from openengine.messaging import ErrorCode, Request, Route, ServiceError, envelopes
from openengine.messaging.errors import invalid_argument

from virtual_control_plane.application.ports import AllocationProvider, Ownership
from virtual_control_plane.application.transactions import CommandIdConflict, UnitOfWork

SCHEMAS = "virtual_control_plane/v1/schemas"
# A first provision may pull an image, boot and clone repositories.
ALLOCATION_TIMEOUT_SECONDS = 600
APPLY = f"{SCHEMAS}/commands.schema.json#/$defs/apply_workspace_command"
ENSURE = f"{SCHEMAS}/commands.schema.json#/$defs/ensure_workspace_command"
# Transient provider failures (retryable errors and timeouts) retry after
# 1, 2, 4, ... seconds, capped, and give up after this many failed attempts.
RETRY_BASE_SECONDS = 1.0
RETRY_CAP_SECONDS = 60.0
MAX_ATTEMPTS = 8

log = logging.getLogger(__name__)


def now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def identity(prefix: str) -> str:
    return prefix + uuid4().hex


def retry_delay(attempts: int) -> float:
    """Seconds to wait after the given number of failed attempts: exponential, capped."""
    return min(RETRY_CAP_SECONDS, RETRY_BASE_SECONDS * 2 ** (attempts - 1))


class Lifecycle:
    def __init__(self, store: UnitOfWork, providers: Mapping[str, AllocationProvider]) -> None:
        self.store, self.providers = store, providers
        self._worker: asyncio.Task | None = None
        self._allocation_lock = asyncio.Lock()

    def commands(self) -> dict[str, Route]:
        return {
            "workspaces.apply": Route(self.apply, receipts=True),
            "workspaces.{workspace_id}.ensure": Route(self.ensure, receipts=True),
        }

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
        return await self._command(request, self._accept)

    async def ensure(self, request: Request) -> dict[str, Any]:
        return await self._command(request, self._accept_ensure)

    async def _command(self, request: Request, accept) -> dict[str, Any]:
        # Server authorizes before invoking these receipt-aware handlers.
        body = request.body
        try:
            async with self.store.transaction() as tx:
                if original := await tx.replay(request.subject, body):
                    return original
                try:
                    response = await accept(tx, request)
                except ServiceError as error:
                    response = {"command_id": body["command_id"], "status": "rejected", "error": error.to_dict()}
                return await tx.record_command(request.subject, body, response)
        except CommandIdConflict:
            raise ServiceError(ErrorCode.COMMAND_ID_CONFLICT, "command ID has different immutable inputs",
                               details={"command_id": body["command_id"], "original_subject": request.subject}) from None

    def _validate(self, body: dict[str, Any], schema: str) -> None:
        envelopes.check(envelopes.COMMAND, body)
        envelopes.check(schema, body)
        if len(body["causal_path"]) >= envelopes.MAX_CAUSAL_PATH:
            raise ServiceError(ErrorCode.CAUSAL_PATH_TOO_LONG, "no room for lifecycle event",
                               details={"max_length": envelopes.MAX_CAUSAL_PATH, "length": len(body["causal_path"]) + 1})
        if body.get("deadline_at") and datetime.fromisoformat(body["deadline_at"]) <= datetime.now(UTC):
            raise ServiceError(ErrorCode.DEADLINE_EXCEEDED, "command expired before acceptance",
                               details={"deadline_at": body["deadline_at"], "received_at": now()})

    async def _accept(self, tx, request: Request) -> dict[str, Any]:
        body = request.body
        self._validate(body, APPLY)
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
        # Provider-specific admission (whole vCPUs, guest path rules) stays in the adapter.
        provider.validate(document)
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
                                                  "operation_id": operation_id, "active_operation_id": operation_id})
        await tx.put("operations", operation_id, {"snapshot": operation, "causal_path": body["causal_path"]})
        await tx.put("workspace_runtimes", workspace_id, {"runtime_id": runtime_id, "generation": 1,
                                                        "provider": spec["provider"], "locator": None})
        await self._event(tx, snapshot, operation, body["causal_path"], "provisioning", {"provider": spec["provider"]})
        return {"command_id": body["command_id"], "status": "accepted", "operation_id": operation_id,
                "resource_id": workspace_id}

    async def _accept_ensure(self, tx, request: Request) -> dict[str, Any]:
        body = request.body
        self._validate(body, ENSURE)
        workspace_id = request.parameters["workspace_id"]
        workspace = await tx.get("workspaces", workspace_id)
        if workspace is None or "snapshot" not in workspace:
            raise ServiceError(ErrorCode.NOT_FOUND, "workspace does not exist",
                               details={"resource_type": "workspace", "resource_id": workspace_id})
        snapshot = workspace["snapshot"]
        expected = body.get("expected_version")
        if expected is not None and expected != snapshot["version"]:
            raise ServiceError(ErrorCode.VERSION_CONFLICT, "workspace version changed",
                               details={"expected_version": expected, "actual_version": snapshot["version"]})
        if active := workspace.get("active_operation_id"):
            # Retryable and unrecorded: the same command ID is accepted once the operation finishes.
            raise ServiceError(ErrorCode.UNAVAILABLE, "another operation on this workspace is in progress",
                               details={"operation_id": active})
        if snapshot["state"] == "suspended" and body["payload"].get("restore", "allow") == "allow":
            raise ServiceError(ErrorCode.UNSUPPORTED_CAPABILITY, "restoring a suspended workspace is not supported",
                               details={"capability": "restore_same_host", "provider": snapshot["provider"]})
        if snapshot["state"] != "ready":
            raise ServiceError(ErrorCode.INVALID_STATE, "only a ready workspace can be ensured",
                               details={"resource_type": "workspace", "resource_id": workspace_id,
                                        "current_state": snapshot["state"], "allowed_states": ["ready"]})
        if snapshot["provider"] not in self.providers:
            raise ServiceError(ErrorCode.UNSUPPORTED_CAPABILITY, "provider is not configured",
                               details={"capability": "provider." + snapshot["provider"]})
        operation_id, timestamp = identity("op_"), now()
        operation = {"operation_id": operation_id, "command_id": body["command_id"], "operation": "workspaces.ensure",
                     "status": "pending", "resources": {"workspace_id": workspace_id},
                     "created_at": timestamp, "updated_at": timestamp}
        workspace["active_operation_id"] = operation_id
        await tx.put("workspaces", workspace_id, workspace)
        await tx.put("operations", operation_id, {"snapshot": operation, "causal_path": body["causal_path"]})
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
        value = await self.store.read(collection, key)
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
        # lock claim each page in memory. Keep pending/running durable until
        # completion: cancellation releases claims and restart retries the same
        # ownership. Operations in retry backoff are skipped until they are due.
        after = ""
        tasks = set()
        exhausted = False
        try:
            while tasks or not exhausted:
                if not exhausted and len(tasks) < 8:
                    async with self.store.transaction() as tx:
                        page = await tx.pending_allocations(8 - len(tasks), after)
                    exhausted = not page
                    due = now()
                    for record in page:
                        after = record["snapshot"]["operation_id"]
                        if record.get("not_before", "") <= due:
                            tasks.add(asyncio.create_task(self._execute(record)))
                if tasks:
                    done, tasks = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        # Child cancellation is a provider failure, not shutdown.
                        # Parent cancellation still propagates from asyncio.wait.
                        if not task.cancelled():
                            task.result()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _execute(self, record) -> None:
        operation = record["snapshot"]
        workspace_id = operation["resources"]["workspace_id"]
        ensure = operation["operation"] == "workspaces.ensure"
        async with self.store.transaction() as tx:
            workspace = await tx.get("workspaces", workspace_id)
            runtime = await tx.get("workspace_runtimes", workspace_id)
            if operation["status"] == "pending":
                # Claim durably: a crash from here on resumes a `running` operation.
                operation.update(status="running", updated_at=now())
                await tx.put("operations", operation["operation_id"], record)
        provider = self.providers.get(runtime["provider"])
        if provider is None:
            # Fail explicitly rather than stalling: the operation lock is released below,
            # unrelated work proceeds, and ownership stays for cleanup once it is back.
            error = ServiceError(ErrorCode.PROVIDER_ERROR, f"provider {runtime['provider']} is not configured on this host",
                                 details={"provider": runtime["provider"]}).to_dict()
            return await self._complete(record, ensure, None, error)
        ownership = Ownership(workspace_id, runtime["runtime_id"], runtime["generation"])
        try:
            async with asyncio.timeout(ALLOCATION_TIMEOUT_SECONDS):
                if ensure:
                    locator = await provider.ensure(ownership, runtime["locator"], workspace["specification"])
                else:
                    locator = await provider.provision(ownership, workspace["specification"])
        except TimeoutError:
            return await self._retry(record, ensure, ServiceError(ErrorCode.UNAVAILABLE, "provider call timed out"))
        except ServiceError as failure:
            if failure.retryable:
                return await self._retry(record, ensure, failure)
            return await self._complete(record, ensure, None, failure.to_dict())
        except Exception:
            log.exception("provider %s failed", operation["operation"])
            verb = "ensure" if ensure else "provision"
            error = ServiceError(ErrorCode.PROVIDER_ERROR, f"provider could not {verb} the workspace").to_dict()
            return await self._complete(record, ensure, None, error)
        await self._complete(record, ensure, locator, None)

    async def _retry(self, record, ensure: bool, failure: ServiceError) -> None:
        attempts = record.get("attempts", 0) + 1
        if attempts >= MAX_ATTEMPTS:
            error = ServiceError(ErrorCode.PROVIDER_ERROR, f"provider failed {attempts} times; last: {failure.message}"[:2048],
                                 details={"attempts": attempts, "last_error": failure.to_dict()}).to_dict()
            return await self._complete(record, ensure, None, error)
        delay = retry_delay(attempts)
        record["attempts"] = attempts
        record["not_before"] = (datetime.now(UTC) + timedelta(seconds=delay)).isoformat().replace("+00:00", "Z")
        record["snapshot"]["updated_at"] = now()
        log.warning("provider %s failed transiently (attempt %d); retrying in %.1fs: %s",
                    record["snapshot"]["operation"], attempts, delay, failure)
        async with self.store.transaction() as tx:
            await tx.put("operations", record["snapshot"]["operation_id"], record)

    async def _complete(self, record, ensure: bool, locator, error) -> None:
        operation = record["snapshot"]
        workspace_id = operation["resources"]["workspace_id"]
        timestamp = now()
        async with self.store.transaction() as tx:
            # The provider ran outside any transaction: merge into the current
            # documents, never the snapshot read before the call.
            workspace = await tx.get("workspaces", workspace_id)
            runtime = await tx.get("workspace_runtimes", workspace_id)
            snapshot = workspace["snapshot"]
            record.pop("attempts", None)
            record.pop("not_before", None)
            operation.update(status="failed" if error else "succeeded", updated_at=timestamp)
            if error:
                operation["error"] = error
            if workspace.get("active_operation_id") == operation["operation_id"]:
                workspace.pop("active_operation_id")
            if ensure:
                # A ready workspace stays as it is: no state change, version bump
                # or second ready event. A failed ensure retains ownership too.
                if not error:
                    operation["result"] = {"runtime_id": runtime["runtime_id"], "runtime_generation": runtime["generation"],
                                           "restored": False}
                    runtime["locator"] = locator
            else:
                snapshot.update(state="failed" if error else "ready", version=snapshot["version"] + 1, updated_at=timestamp)
                if error:
                    snapshot["failure"] = error
                    payload = {"error": error, "runtime_generation": runtime["generation"]}
                else:
                    runtime["locator"] = locator
                    snapshot["runtime"] = {"runtime_id": runtime["runtime_id"], "generation": runtime["generation"],
                                           "state": "running", "started_at": timestamp}
                    operation["result"] = {"workspace_id": workspace_id, "created": True,
                                           "runtime_id": runtime["runtime_id"], "runtime_generation": runtime["generation"]}
                    payload = {"runtime_generation": runtime["generation"]}
                await self._event(tx, snapshot, operation, record["causal_path"], snapshot["state"], payload)
            await tx.put("workspaces", workspace_id, workspace)
            await tx.put("operations", operation["operation_id"], record)
            await tx.put("workspace_runtimes", workspace_id, runtime)

    def start(self) -> None:
        self._worker = asyncio.create_task(self._run())

    async def _run(self) -> None:
        while True:
            try:
                await self.reconcile()
            except Exception:
                log.exception("workspace reconciliation failed; retrying")
            await asyncio.sleep(1)

    async def close(self) -> None:
        if self._worker:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
