"""Opt-in durable acceptance. No runtime transport or production enable switch.

Trusted context adapters validate executor configuration and resolve persisted
workspace context. The application validates wire structure and filesystem paths
before its first acceptance write. Runtime readiness is committed by a future
registration handler; this slice seeds it only in deterministic tests.
"""
from __future__ import annotations

import base64
import binascii
import json
from copy import deepcopy
from pathlib import Path

from openengine.contracts.runtime import validate_wire_message
from openengine.messaging import ErrorCode, Request, Route, ServiceError, envelopes
from openengine.messaging.errors import invalid_argument

from virtual_control_plane.application.lifecycle import Lifecycle, SCHEMAS, accepted, identity, new_operation, now
from virtual_control_plane.application.ports import ExecutionContextPort
from virtual_control_plane.application.transactions import CommandIdConflict


class InvalidTransition(ValueError):
    """An unknown, stale or terminal attempt cannot make this transition."""


class Execution:
    def __init__(self, lifecycle: Lifecycle, context: ExecutionContextPort | None = None, *, enabled: bool = False):
        self.lifecycle, self.store = lifecycle, lifecycle.store
        self.context, self.enabled = context, enabled

    def commands(self):
        return {"workspaces.{workspace_id}.execute": Route(self.execute, receipts=True)} if self.enabled else {}

    def queries(self):
        return {key: Route(handler, f"{SCHEMAS}/queries.schema.json#/$defs/{schema}")
                for key, handler, schema in [
                    ("sessions.{session_id}.get", self.get, "get_session_query"),
                    ("workspaces.{workspace_id}.sessions.list", self.list, "list_sessions_query")]}

    async def execute(self, request: Request):
        prepared = None
        failure = None
        try:
            # A short preflight reservation checks replay before invoking adapters.
            # Never await context preparation while holding the writer.
            async with self.store.transaction() as tx:
                if original := await tx.replay(request.subject, request.body):
                    return original
                _, record, _, runtime, _ = await self._admit(tx, request)
            root, context = await self.context.prepare(
                deepcopy(record), deepcopy(runtime), deepcopy(request.body["payload"]))
            self._paths(root, context)
            prepared = (record, runtime, context)
        except (ServiceError, CommandIdConflict) as error:
            failure = error

        async def accept(tx, current):
            if failure is not None:
                raise failure
            return await self._accept(tx, current, prepared)

        # Replay is checked again, including if another retry won during prepare.
        return await self.lifecycle._command(request, accept)

    async def _admit(self, tx, request):
        body = request.body
        self.lifecycle._validate(body, f"{SCHEMAS}/commands.schema.json#/$defs/execute_command")
        if not self.enabled or self.context is None:
            raise ServiceError(ErrorCode.UNSUPPORTED_CAPABILITY, "execution delivery is disabled",
                               details={"capability": "execute"})
        workspace_id, record, workspace = await self.lifecycle._load_workspace(tx, request)
        self.lifecycle._require_idle(record)
        runtime = await tx.get("workspace_runtimes", workspace_id)
        current = workspace.get("runtime", {})
        if (workspace["state"] != "ready" or current.get("state") != "running" or not runtime
                or current.get("runtime_id") != runtime["runtime_id"]
                or current.get("generation") != runtime["generation"] or not runtime.get("locator")):
            raise ServiceError(ErrorCode.INVALID_STATE, "execute requires a ready owned runtime",
                               details={"resource_type": "workspace", "resource_id": workspace_id,
                                        "current_state": workspace["state"], "allowed_states": ["ready"]})
        status = await tx.get("runtime_status", runtime["runtime_id"])
        if (not status or not status.get("ready") or status.get("quarantined")
                or status.get("workspace_id") != workspace_id or status.get("generation") != runtime["generation"]):
            raise ServiceError(ErrorCode.UNAVAILABLE, "runtime has not registered execution readiness")
        payload, timestamp = body["payload"], now()
        if payload["agent"]["runner"] not in status.get("executors", []):
            raise ServiceError(ErrorCode.UNSUPPORTED_CAPABILITY, "executor is not registered",
                               details={"capability": "executor." + payload["agent"]["runner"]})
        if len(workspace["active_attempt_ids"]) >= status.get("max_concurrent_attempts", 0):
            raise ServiceError(ErrorCode.UNAVAILABLE, "runtime concurrency limit reached")
        selection = payload["session"]
        if selection["mode"] == "new":
            session = {"session_id": identity("session_"), "workspace_id": workspace_id,
                       "runtime_generation": runtime["generation"], "agent": deepcopy(payload["agent"]),
                       "state": "active", "attempts": [], "created_at": timestamp, "updated_at": timestamp}
        else:
            stored = await tx.get("sessions", selection["session_id"])
            if not stored or stored["snapshot"]["workspace_id"] != workspace_id:
                raise ServiceError(ErrorCode.NOT_FOUND, "session does not exist in this workspace",
                                   details={"resource_type": "session", "resource_id": selection["session_id"]})
            session = stored["snapshot"]
            if session["state"] != "idle" or session["runtime_generation"] != runtime["generation"]:
                raise ServiceError(ErrorCode.INVALID_STATE, "session must be idle in the current generation",
                                   details={"resource_type": "session", "resource_id": session["session_id"],
                                            "current_state": session["state"], "allowed_states": ["idle"]})
            if session["agent"] != payload["agent"]:
                raise invalid_argument([("agent", "continuation requires the same agent configuration")])
        return workspace_id, record, workspace, runtime, session

    async def _accept(self, tx, request, prepared):
        workspace_id, record, workspace, runtime, session = await self._admit(tx, request)
        previous, owned_runtime, context = prepared
        # Preparation belongs to this exact persisted context and owned runtime.
        # Active attempts/session/capacity are independently rechecked by _admit.
        if record["specification"] != previous["specification"] or runtime != owned_runtime:
            raise ServiceError(ErrorCode.UNAVAILABLE, "execution context changed during preparation")
        body, payload, timestamp = request.body, request.body["payload"], now()
        selection = payload["session"]
        attempt_id, command_id = identity("attempt_"), identity("rtc_")
        binding = {"workspace_id": workspace_id, "runtime_id": runtime["runtime_id"],
                   "runtime_generation": runtime["generation"], "session_id": session["session_id"], "attempt_id": attempt_id}
        operation = new_operation(body, "workspaces.execute", workspace_id, timestamp)
        operation["resources"] = {key: value for key, value in binding.items() if key.endswith("_id")}
        message = {"command_id": command_id, "kind": "execute", "target": binding,
                   "execution": {"command_id": command_id, "cause": {
                       "operation": {key: operation[key] for key in ("operation_id", "command_id")},
                       "causal_path": body["causal_path"]}},
                   "payload": {"agent": payload["agent"], "task": payload["task"],
                               "session_mode": selection["mode"], "context": context}}
        subject = f"oe.internal.v1.runtime.runtimes.{runtime['runtime_id']}.execute"
        wire = envelopes.encode(message)
        if errors := validate_wire_message("execute", wire, subject):
            raise invalid_argument([("context", "; ".join(errors))])
        attempt = {key: binding[key] for key in ("workspace_id", "session_id", "attempt_id", "runtime_generation")}
        attempt.update(state="starting", instruction=payload["task"]["instruction"])
        session.update(state="active", updated_at=timestamp, attempts=[attempt, *session["attempts"]][:20])
        workspace["active_attempt_ids"].append(attempt_id)
        workspace.update(version=workspace["version"] + 1, updated_at=timestamp)
        # All validation precedes the first write. Any storage failure rolls back
        # these records and the receipt written by Lifecycle._command.
        await tx.put("sessions", session["session_id"], {"snapshot": session})
        await tx.put("attempts", attempt_id, {"snapshot": attempt, "operation_id": operation["operation_id"]})
        await tx.put("attempt_bindings", attempt_id, {**binding, "execution": message["execution"], "session_mode": selection["mode"]})
        await tx.put("operations", operation["operation_id"], {"snapshot": operation, "causal_path": body["causal_path"]})
        await tx.put("workspaces", workspace_id, record)
        await tx.append_runtime_command(command_id, subject, wire)
        return accepted(body, operation["operation_id"], attempt_id)

    @staticmethod
    def _paths(root, context):
        try:
            root = Path(root).resolve(strict=True)
            if not root.is_dir() or context["references"]:
                raise ValueError()  # noqa: TRY301 -- normalize path failures
            for value, directory in [(context["working_directory"], True),
                                     *((value, False) for value in context["instruction_files"])]:
                path = (root / value).resolve(strict=True)
                if not path.is_relative_to(root) or (not path.is_dir() if directory else not path.is_file()):
                    raise ValueError()  # noqa: TRY301 -- normalize path failures
        except (OSError, ValueError, RuntimeError):
            raise invalid_argument([("context", "paths must exist inside the authorized workspace root")]) from None

    async def get(self, request):
        return {"session": await self.lifecycle._read("sessions", request.parameters["session_id"])}

    async def list(self, request):
        workspace_id = request.parameters["workspace_id"]
        await self.lifecycle._read("workspaces", workspace_id)
        params = request.body.get("params", {})
        filters = {"workspace_id": workspace_id, "state": sorted(params.get("state", []))}
        after = None
        if cursor := params.get("cursor"):
            try:
                decoded = json.loads(base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True))
                after = decoded["after"]
                if decoded["version"] != 1 or decoded["filters"] != filters or not isinstance(after, list) or len(after) != 2 or not all(isinstance(v, str) for v in after):
                    raise ValueError()  # noqa: TRY301 -- normalize cursor failures
            except (binascii.Error, ValueError, TypeError, KeyError):
                raise ServiceError(ErrorCode.INVALID_CURSOR, "invalid session cursor", details={"reason": "malformed"}) from None
        def key(session):
            return [session["created_at"], session["session_id"]]
        limit = int(params.get("limit", 50))
        sessions = await self.store.session_page(filters, after, limit + 1)
        page = sessions[:limit]
        cursor = None
        if len(sessions) > limit:
            cursor = base64.urlsafe_b64encode(json.dumps({"version": 1, "filters": filters, "after": key(page[-1])}).encode()).decode().rstrip("=")
        return {"items": page, "next_cursor": cursor}

    async def transition(self, tx, attempt_id, state, *, error=None, result=None):
        """Trusted projection primitive, not a runtime message handler.

        A future delivery adapter must authenticate, fence, order and deduplicate
        updates before invoking this in its update transaction. An acknowledgement
        has no transition: only attempt.started projects task.started and succeeds
        execute. This slice deliberately exposes no runtime subscription.
        """
        record = await tx.get("attempts", attempt_id)
        if record is None:
            raise InvalidTransition()
        attempt = record["snapshot"]
        allowed = {"starting": {"running", "failed"}, "running": {"completed", "failed"}}
        if state not in allowed.get(attempt["state"], set()):
            raise InvalidTransition()
        if (state == "failed" and error is None) or (state == "completed" and result is None):
            raise InvalidTransition()
        binding = await tx.get("attempt_bindings", attempt_id)
        runtime = await tx.get("workspace_runtimes", binding["workspace_id"])
        if not runtime or runtime["runtime_id"] != binding["runtime_id"] or runtime["generation"] != binding["runtime_generation"]:
            raise InvalidTransition()
        timestamp = now()
        workspace_record = await tx.get("workspaces", binding["workspace_id"])
        workspace = workspace_record["snapshot"]
        session_record = await tx.get("sessions", binding["session_id"])
        session = session_record["snapshot"]
        operation_record = await tx.get("operations", record["operation_id"])
        operation = operation_record["snapshot"]
        attempt["state"] = state
        payload = {"operation": binding["execution"]["cause"]["operation"]}
        if state == "running":
            attempt["started_at"] = timestamp
            operation.update(status="succeeded", updated_at=timestamp, result={
                "attempt_id": attempt_id, "session_id": binding["session_id"],
                "runtime_generation": binding["runtime_generation"], "restored": False})
            payload.update(runtime_generation=binding["runtime_generation"], session_mode=binding["session_mode"])
        else:
            attempt["ended_at"] = timestamp
            if error is not None:
                attempt["error"] = error
                payload["error"] = error
            if result is not None:
                attempt["result"] = result
                payload["result"] = result
            if operation["status"] == "pending":
                operation.update(status="failed", updated_at=timestamp, error=error)
            session["state"] = "idle"
            workspace["active_attempt_ids"].remove(attempt_id)
            workspace.update(version=workspace["version"] + 1, updated_at=timestamp)
        session.update(updated_at=timestamp, attempts=[attempt if a["attempt_id"] == attempt_id else a for a in session["attempts"]])
        event_id = identity("evt_")
        await tx.append_event({"event_id": event_id, "event_type": "task." + ("started" if state == "running" else state),
                               "occurred_at": timestamp, "producer": "vcp",
                               "causal_path": [*operation_record["causal_path"], event_id],
                               "resources": {key: binding[key] for key in ("workspace_id", "session_id", "attempt_id")},
                               "payload": payload})
        await tx.put("attempts", attempt_id, record)
        await tx.put("sessions", binding["session_id"], session_record)
        await tx.put("workspaces", binding["workspace_id"], workspace_record)
        await tx.put("operations", operation["operation_id"], operation_record)
