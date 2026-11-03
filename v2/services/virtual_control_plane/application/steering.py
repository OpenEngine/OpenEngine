"""Fake-only active-attempt steering and append-only session history queries."""
from __future__ import annotations

import re
import time
from copy import deepcopy

from openengine.messaging import ErrorCode, ServiceError, envelopes

from virtual_control_plane.application.lifecycle import SCHEMAS, accepted, identity, new_operation, now

BINDING = ("workspace_id", "runtime_id", "runtime_generation", "session_id", "attempt_id")
CURSOR = re.compile(r"v1_[0-9a-f]{32}\Z")


def invalid_state(resource_type, resource_id, state, allowed):
    return ServiceError(ErrorCode.INVALID_STATE, "steering requires a current running attempt",
                        details={"resource_type": resource_type, "resource_id": resource_id,
                                 "current_state": state, "allowed_states": allowed})


def failed(attempt_id):
    return invalid_state("attempt", attempt_id, "ended", ["running"]).to_dict()


class Steering:
    def __init__(self, execution):
        self.execution, self.lifecycle, self.store = execution, execution.lifecycle, execution.store

    async def steer(self, request):
        return await self.lifecycle._command(request, self._accept)

    async def _accept(self, tx, request):
        body, payload = request.body, request.body["payload"]
        self.lifecycle._validate(body, f"{SCHEMAS}/commands.schema.json#/$defs/steer_command")
        workspace_id, record, workspace = await self.lifecycle._load_workspace(tx, request)
        self.lifecycle._require_idle(record)
        session_id = payload["session_id"]
        stored = await tx.get("sessions", session_id)
        if not stored or stored["snapshot"]["workspace_id"] != workspace_id:
            raise ServiceError(ErrorCode.NOT_FOUND, "session does not exist in this workspace",
                               details={"resource_type": "session", "resource_id": session_id})
        session = stored["snapshot"]
        # Codex is rejected even if a runtime mistakenly advertises it.
        runner = session["agent"]["runner"]
        if runner != "fake":
            raise ServiceError(ErrorCode.UNSUPPORTED_CAPABILITY, "executor does not support steering",
                               details={"capability": "steer.active." + runner})
        runtime, current = await tx.get("workspace_runtimes", workspace_id), workspace.get("runtime", {})
        if workspace["state"] == "suspended" and payload.get("restore", "allow") == "allow":
            raise ServiceError(ErrorCode.UNSUPPORTED_CAPABILITY, "steering cannot restore a workspace",
                               details={"capability": "restore"})
        if (workspace["state"] != "ready" or current.get("state") != "running" or not runtime
                or current.get("runtime_id") != runtime["runtime_id"] or current.get("generation") != runtime["generation"]
                or session["runtime_generation"] != runtime["generation"]):
            raise invalid_state("workspace", workspace_id, workspace["state"], ["ready"])
        attempts = [a for a in session["attempts"] if a["state"] in ("starting", "running", "cancelling")]
        if session["state"] != "active" or len(attempts) != 1 or attempts[0]["state"] != "running":
            raise invalid_state("session", session_id, session["state"], ["active"])
        attempt_id = attempts[0]["attempt_id"]
        attempt = await tx.get("attempts", attempt_id)
        binding = await tx.get("attempt_bindings", attempt_id)
        if (not attempt or attempt["snapshot"]["state"] != "running" or not binding
                or binding["runtime_id"] != runtime["runtime_id"] or binding["runtime_generation"] != runtime["generation"]
                or binding["session_id"] != session_id or binding["workspace_id"] != workspace_id
                or attempt_id not in workspace["active_attempt_ids"]):
            raise invalid_state("attempt", attempt_id, "unowned", ["running"])
        status = await tx.get("runtime_status", runtime["runtime_id"])
        if (not status or not status.get("ready") or status.get("quarantined")
                or status.get("workspace_id") != workspace_id or status.get("generation") != runtime["generation"]):
            raise ServiceError(ErrorCode.UNAVAILABLE, "runtime has not registered steering readiness")
        if runner not in status.get("steering_executors", []):
            raise ServiceError(ErrorCode.UNSUPPORTED_CAPABILITY, "executor does not advertise steering",
                               details={"capability": "steer.active." + runner})
        operation = new_operation(body, "workspaces.steer", workspace_id, now())
        operation["resources"] = {key: binding[key] for key in BINDING if key.endswith("_id")}
        command_id = identity("rtc_")
        message = {"command_id": command_id, "kind": "steer", "target": {key: binding[key] for key in BINDING},
                   "execution": binding["execution"], "cause": self.execution._cause(operation, body),
                   "payload": {"message": payload["message"]}}
        runtime_command = self.execution._runtime_command(message, attempt_id, "message")
        await tx.put("operations", operation["operation_id"], {"snapshot": operation, "causal_path": body["causal_path"]})
        await tx.put("steering_commands", command_id, {**deepcopy(message), "status": "pending"})
        await tx.append_runtime_command(*runtime_command)
        return accepted(body, operation["operation_id"], session_id)

    async def project(self, tx, command_id, outcome, error=None, *, target=None, cause=None):
        command = await tx.get("steering_commands", command_id)
        if not command or (target is not None and (command["target"] != target or command["cause"] != cause)):
            return False
        if command["status"] != "pending":
            return True  # a late update can never reverse or repeat a terminal projection
        binding, timestamp = command["target"], now()
        operation_id = command["cause"]["operation"]["operation_id"]
        record = await tx.get("operations", operation_id)
        operation = record["snapshot"]
        payload = {"operation": command["cause"]["operation"]}
        if outcome == "applied":
            operation.update(status="succeeded", updated_at=timestamp, result={
                "session_id": binding["session_id"], "attempt_id": binding["attempt_id"],
                "runtime_generation": binding["runtime_generation"], "restored": False})
            payload["runtime_generation"] = binding["runtime_generation"]
        else:
            error = error or failed(binding["attempt_id"])
            operation.update(status="failed", updated_at=timestamp, error=error)
            payload["error"] = error
        event_id = identity("evt_")
        await tx.append_event({"event_id": event_id, "event_type": "steering." + outcome,
                               "occurred_at": timestamp, "producer": "vcp",
                               "causal_path": [*command["cause"]["causal_path"], event_id],
                               "resources": {key: binding[key] for key in ("workspace_id", "session_id", "attempt_id")},
                               "payload": payload})
        await tx.append_history(binding["session_id"], {
            "kind": "steering", "attempt_id": binding["attempt_id"], "occurred_at": timestamp,
            "message": command["payload"]["message"], "status": outcome, "operation_id": operation_id})
        command["status"] = outcome
        await tx.put("steering_commands", command_id, command)
        await tx.put("operations", operation_id, record)
        # Stops even undelivered commands. A racing acknowledgement cannot reopen delivery.
        await tx.retire_runtime_command(command_id)
        return True

    async def terminate(self, tx, attempt_id):
        for command in await tx.pending_steering(attempt_id):
            await self.project(tx, command["command_id"], "failed")

    async def history(self, request):
        session_id, params = request.parameters["session_id"], request.body.get("params", {})
        filters = {"kind": sorted(params.get("kind", [])), "attempt_id": params.get("attempt_id")}
        scope = {"tenant": request.principal.tenant_id if request.principal else None,
                 "session_id": session_id, "filters": filters}
        after = 0
        if cursor := params.get("cursor"):
            stored = await self.store.read("history_cursors", cursor) if CURSOR.fullmatch(cursor) else None
            if not stored or stored["scope"] != scope or stored.get("expires_at", float("inf")) < time.time():
                raise ServiceError(ErrorCode.INVALID_CURSOR, "invalid session history cursor", details={"reason": "malformed"})
            after = stored["after"]
        await self.lifecycle._read("sessions", session_id)
        limit = int(params.get("limit", 50))
        entries = await self.store.history_page(session_id, filters, after, limit + 1)
        # Reserve ample space for the query envelope below NATS's default 1 MiB.
        page, size, cursor = [], 0, None
        for entry in entries[:limit]:
            encoded_size = len(envelopes.encode(entry)) + 1
            # The page budget is soft for a single entry: returning it alone
            # lets clients advance past transport-sized entries above the budget.
            if page and size + encoded_size > 900 * 1024:
                break
            page.append(entry)
            size += encoded_size
        if page and len(entries) > len(page):
            cursor = identity("v1_")
            async with self.store.transaction() as tx:
                await tx.save_history_cursor(cursor, {"scope": scope, "after": page[-1]["sequence"]})
        return {"items": page, "next_cursor": cursor}
