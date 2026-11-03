"""Opt-in durable acceptance. No production enable switch.

Trusted context adapters validate executor configuration and resolve persisted
workspace context. The application validates wire structure and filesystem paths
before its first acceptance write. Runtime readiness is committed by the
daemon's `runtime.ready` update (`runtime_delivery.py`), which also delivers the
accepted command bytes and projects ordered updates through `transition`.

Cancel atomically writes its receipt, a pending `workspaces.cancel` operation,
the attempt's `cancelling` state and one immutable internal cancel command.
The attempt stays in `active_attempt_ids`, so its session, capacity and the
workspace's destroy guard are kept until the daemon's terminal update confirms
termination. Later cancels of a cancelling attempt are coalesced onto the first
(winning) command; a cancel of an ended attempt succeeds at once with its
outcome. The terminal projection settles every pending cancel operation.
"""
from __future__ import annotations

import base64
import binascii
import json
from copy import deepcopy
from pathlib import Path

from openengine.contracts.runtime import timestamp_key, validate_wire_message
from openengine.messaging import ErrorCode, Request, Route, ServiceError, envelopes
from openengine.messaging.errors import invalid_argument

from virtual_control_plane.application.lifecycle import Lifecycle, SCHEMAS, accepted, identity, new_operation, now
from virtual_control_plane.application.ports import ExecutionContextPort
from virtual_control_plane.application.transactions import CommandIdConflict


CANCEL = f"{SCHEMAS}/commands.schema.json#/$defs/cancel_command"
TERMINAL = ("completed", "failed", "cancelled")


class InvalidTransition(ValueError):
    """An unknown, stale or terminal attempt cannot make this transition."""


class Execution:
    def __init__(self, lifecycle: Lifecycle, context: ExecutionContextPort | None = None, *, enabled: bool = False):
        self.lifecycle, self.store = lifecycle, lifecycle.store
        self.context, self.enabled = context, enabled

    def commands(self):
        if not self.enabled:
            return {}
        return {"workspaces.{workspace_id}.execute": Route(self.execute, receipts=True),
                "workspaces.{workspace_id}.cancel": Route(self.cancel, receipts=True)}

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
                   "execution": {"command_id": command_id, "cause": self._cause(operation, body)},
                   "payload": {"agent": payload["agent"], "task": payload["task"],
                               "session_mode": selection["mode"], "context": context}}
        runtime_command = self._runtime_command(message, attempt_id, "context")
        attempt = {key: binding[key] for key in ("workspace_id", "session_id", "attempt_id", "runtime_generation")}
        attempt.update(state="starting", instruction=payload["task"]["instruction"])
        session.update(state="active", updated_at=timestamp, attempts=[attempt, *session["attempts"]][:20])
        workspace["active_attempt_ids"].append(attempt_id)
        workspace.update(version=workspace["version"] + 1, updated_at=timestamp)
        # All validation precedes the first write. Any storage failure rolls back
        # these records and the receipt written by Lifecycle._command.
        stored = await tx.get("sessions", session["session_id"]) or {}  # keeps its history sequence
        await tx.put("sessions", session["session_id"], {**stored, "snapshot": session})
        await tx.put("attempts", attempt_id, {"snapshot": attempt, "operation_id": operation["operation_id"]})
        await tx.put("attempt_bindings", attempt_id, {**binding, "execution": message["execution"], "session_mode": selection["mode"]})
        await tx.put("operations", operation["operation_id"], {"snapshot": operation, "causal_path": body["causal_path"]})
        await tx.put("workspaces", workspace_id, record)
        await tx.append_runtime_command(*runtime_command)
        return accepted(body, operation["operation_id"], attempt_id)

    @staticmethod
    def _cause(operation, body):
        return {"operation": {key: operation[key] for key in ("operation_id", "command_id")},
                "causal_path": body["causal_path"]}

    @staticmethod
    def _runtime_command(message, attempt_id, field):
        """Validated `append_runtime_command` arguments for one immutable internal runtime command."""
        subject = f"oe.internal.v1.runtime.runtimes.{message['target']['runtime_id']}.{message['kind']}"
        wire = envelopes.encode(message)
        if errors := validate_wire_message(message["kind"], wire, subject):
            raise invalid_argument([(field, "; ".join(errors))])
        return message["command_id"], attempt_id, subject, wire

    @staticmethod
    def _replace_attempt(session, attempt, timestamp):
        session.update(updated_at=timestamp, attempts=[attempt if a["attempt_id"] == attempt["attempt_id"] else a
                                                       for a in session["attempts"]])

    async def cancel(self, request: Request):
        return await self.lifecycle._command(request, self._cancel)

    async def _cancel(self, tx, request):
        body = request.body
        self.lifecycle._validate(body, CANCEL)
        workspace_id, _, _ = await self.lifecycle._load_workspace(tx, request)
        attempt_id = body["payload"]["attempt_id"]
        record, binding = await tx.get("attempts", attempt_id), await tx.get("attempt_bindings", attempt_id)
        if not record or not binding or binding["workspace_id"] != workspace_id:
            raise ServiceError(ErrorCode.NOT_FOUND, "attempt does not exist in this workspace",
                               details={"resource_type": "attempt", "resource_id": attempt_id})
        attempt, timestamp = record["snapshot"], now()
        operation = new_operation(body, "workspaces.cancel", workspace_id, timestamp)
        operation["resources"] = {key: binding[key] for key in ("workspace_id", "runtime_id", "session_id", "attempt_id")}
        if attempt["state"] in TERMINAL:
            # It ended first and keeps its outcome; there is nothing to stop.
            operation.update(status="succeeded", result={"attempt_id": attempt_id, "final_state": attempt["state"]})
        elif record.get("cancel"):
            record["cancel"]["operation_ids"].append(operation["operation_id"])  # coalesced onto the winning cancel
        else:
            runtime = await tx.get("workspace_runtimes", workspace_id)
            if not runtime or runtime["runtime_id"] != binding["runtime_id"] or runtime["generation"] != binding["runtime_generation"]:
                raise ServiceError(ErrorCode.INVALID_STATE, "the attempt's runtime is no longer owned",
                                   details={"resource_type": "attempt", "resource_id": attempt_id,
                                            "current_state": attempt["state"], "allowed_states": ["starting", "running"]})
            command_id = identity("rtc_")
            cause = self._cause(operation, body)
            message = {"command_id": command_id, "kind": "cancel",
                       "target": {key: binding[key] for key in ("workspace_id", "runtime_id", "runtime_generation",
                                                                 "session_id", "attempt_id")},
                       "execution": binding["execution"], "cause": cause,
                       "payload": {key: body["payload"][key] for key in ("reason",) if key in body["payload"]}}
            runtime_command = self._runtime_command(message, attempt_id, "payload")
            record["cancel"] = {"command_id": command_id, "cause": cause, "operation_ids": [operation["operation_id"]]}
            attempt["state"] = "cancelling"
            session_record = await tx.get("sessions", binding["session_id"])
            session = session_record["snapshot"]
            self._replace_attempt(session, attempt, timestamp)
            await tx.put("sessions", binding["session_id"], session_record)
            await tx.append_runtime_command(*runtime_command)
        await tx.put("attempts", attempt_id, record)
        await tx.put("operations", operation["operation_id"], {"snapshot": operation, "causal_path": body["causal_path"]})
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
        except (OSError, ValueError, RuntimeError, KeyError, TypeError):
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

    async def progress(self, tx, attempt_id, payload):
        """Trusted projection of one ordered, deduplicated progress record for a running attempt.

        A conversation record takes the session's next history sequence; a usage
        delta is added to the attempt once, and its period may not overlap the
        attempt's previous one. Nothing is projected for artifacts here.
        """
        record = await tx.get("attempts", attempt_id)
        if record is None or record["snapshot"]["state"] not in ("running", "cancelling") or "started_at" not in record["snapshot"]:
            raise InvalidTransition()
        binding = await tx.get("attempt_bindings", attempt_id)
        attempt, timestamp, event_id = record["snapshot"], now(), identity("evt_")
        resources = {key: binding[key] for key in ("workspace_id", "session_id", "attempt_id")}
        session_record = await tx.get("sessions", binding["session_id"])
        session = session_record["snapshot"]
        if payload["kind"] == "conversation":
            session_record["history_sequence"] = sequence = session_record.get("history_sequence", 0) + 1
            event = {"event_type": "conversation.message", "resources": {**resources, "message_id": payload["message_id"]},
                     "payload": {"role": payload["role"], "text": payload["text"], "sequence": sequence}}
        elif payload["kind"] == "usage":
            # Compared as strings so the contract's nanoseconds are not truncated to microseconds.
            start, end = (timestamp_key(payload[key]) for key in ("period_start", "period_end"))
            previous = record.get("usage_period_end")
            if end < start or (previous and start < timestamp_key(previous)):
                raise InvalidTransition()
            total = attempt.setdefault("usage", {"input_tokens": 0, "output_tokens": 0})
            for key, value in payload["usage"].items():
                total[key] = total.get(key, 0) + value
            record["usage_period_end"] = payload["period_end"]
            event = {"event_type": "usage.reported", "resources": resources, "payload": {
                key: payload[key] for key in ("usage", "period_start", "period_end")}}
        else:
            raise InvalidTransition()
        operation_record = await tx.get("operations", record["operation_id"])
        self._replace_attempt(session, attempt, timestamp)
        await tx.append_event({"event_id": event_id, "occurred_at": timestamp, "producer": "vcp",
                               "causal_path": [*operation_record["causal_path"], event_id], **event})
        await tx.put("attempts", attempt_id, record)
        await tx.put("sessions", binding["session_id"], session_record)

    async def transition(self, tx, attempt_id, state, *, error=None, result=None, cancel=None):
        """Trusted projection primitive, not a runtime message handler.

        `RuntimeUpdates` fences, orders and deduplicates updates before invoking
        this in its update transaction. An acknowledgement has no transition:
        only attempt.started projects task.started and succeeds execute. A
        cancelling attempt that starts stays cancelling; it ends cancelled only
        with the winning cancel's `cancel` (internal command ID and cause), or
        keeps the completed/failed outcome the daemon reached first.
        """
        record = await tx.get("attempts", attempt_id)
        if record is None:
            raise InvalidTransition()
        attempt = record["snapshot"]
        allowed = {"starting": {"running", "failed"}, "running": {"completed", "failed"},
                   "cancelling": {"running", "completed", "failed", "cancelled"}}
        if state not in allowed.get(attempt["state"], set()):
            raise InvalidTransition()
        if (state == "failed" and error is None) or (state == "completed" and result is None):
            raise InvalidTransition()
        winning = record.get("cancel")
        if (state == "running" and "started_at" in attempt) or (state == "cancelled" and (
                not winning or not cancel or {key: cancel.get(key) for key in ("command_id", "cause")}
                != {key: winning[key] for key in ("command_id", "cause")})):
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
        attempt["state"] = "cancelling" if state == "running" and winning else state
        payload = {"operation": binding["execution"]["cause"]["operation"]}
        causal_path = operation_record["causal_path"]
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
            if state == "cancelled":
                payload["cancelled_by"] = winning["cause"]["operation"]
                if reason := cancel.get("reason"):
                    payload["reason"] = reason
                causal_path = winning["cause"]["causal_path"]
                error = {"code": "INVALID_STATE", "message": "attempt was cancelled before it started", "retryable": False,
                         "details": {"resource_type": "attempt", "resource_id": attempt_id,
                                     "current_state": "cancelled", "allowed_states": ["running"]}}
            if operation["status"] == "pending":
                operation.update(status="failed", updated_at=timestamp, error=error)
            for cancel_id in (winning or {}).get("operation_ids", []):
                cancel_record = await tx.get("operations", cancel_id)
                cancel_record["snapshot"].update(status="succeeded", updated_at=timestamp,
                                                 result={"attempt_id": attempt_id, "final_state": state})
                await tx.put("operations", cancel_id, cancel_record)
            session["state"] = "idle"
            workspace["active_attempt_ids"].remove(attempt_id)
            workspace.update(version=workspace["version"] + 1, updated_at=timestamp)
        self._replace_attempt(session, attempt, timestamp)
        event_id = identity("evt_")
        await tx.append_event({"event_id": event_id, "event_type": "task." + ("started" if state == "running" else state),
                               "occurred_at": timestamp, "producer": "vcp",
                               "causal_path": [*causal_path, event_id],
                               "resources": {key: binding[key] for key in ("workspace_id", "session_id", "attempt_id")},
                               "payload": payload})
        await tx.put("attempts", attempt_id, record)
        await tx.put("sessions", binding["session_id"], session_record)
        await tx.put("workspaces", binding["workspace_id"], workspace_record)
        await tx.put("operations", operation["operation_id"], operation_record)
