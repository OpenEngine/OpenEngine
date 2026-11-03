"""Durable command reception, ordered updates and restart recovery for one runtime generation.

The provisioned binding (workspace, runtime, generation) is immutable for the
process and checked before any receipt lookup: a wrong runtime is
`IDENTITY_MISMATCH`, a wrong generation `STALE_RUNTIME`, and neither touches
the journal. Accepting execute journals its receipt and a `recorded` attempt
in one transaction before replying; the reply means durable ownership, not
that work started. Accepting cancel journals its receipt and the attempt's
cancel intent together: a `recorded` attempt (or, before execute arrived, a
tombstone for its exact binding) is cancelled `never_started` at once, and a
launching or running one becomes `cancelling`, keeping its session and
capacity until the worker confirms termination. A matching execute arriving
after a tombstone is acknowledged and never launched. The first cancel wins:
every later terminal record of that attempt is `attempt.cancelled` with its
cause, and an attempt that already ended keeps its outcome. Every update is encoded, validated and journaled with its
sequence before it is sent. Progress waits while the unacknowledged outbox is
full, so a fast producer is slowed rather than dropped; terminal updates never
wait. Inspect reads without reserving the writer.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from openengine.contracts.runtime import digest, error, validate_wire_message

from workspace_daemon import context as task_context
from workspace_daemon.executor import ExecutorPort
from workspace_daemon.initialization import RecoveryRefused, Refusal
from workspace_daemon.store import TERMINAL, JournalStore

log = logging.getLogger(__name__)

INTERRUPTED = ("launching", "running", "cancelling")


def identity(prefix: str) -> str:
    return prefix + uuid4().hex


def now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def encode(value: dict[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()


def mismatch(binding: Mapping[str, Any], subject: str, body: dict[str, Any], action: str) -> dict[str, Any] | None:
    """Wire claims against the provisioned binding; claims never authenticate themselves."""
    target = body["target"]
    if (subject != f"oe.internal.v1.runtime.runtimes.{binding['runtime_id']}.{action}"
            or target["workspace_id"] != binding["workspace_id"] or target["runtime_id"] != binding["runtime_id"]):
        return error("IDENTITY_MISMATCH", "command is not for this runtime")
    if target["runtime_generation"] != binding["runtime_generation"]:
        return error("STALE_RUNTIME", "runtime generation is not authorized")
    return None


class UnsendableUpdate(ValueError):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))


class Journal:
    def __init__(self, store: JournalStore, binding: dict[str, Any], root: Path,
                 executors: Mapping[str, ExecutorPort], *, max_concurrent_attempts: int = 1,
                 established: bool = False, max_pending_updates: int = 1000) -> None:
        self.store, self.binding, self.root = store, dict(binding), root
        self.executors, self.max_concurrent_attempts = executors, max_concurrent_attempts
        self.max_pending_updates, self.backpressure_seconds = max_pending_updates, 0.05
        self.established = established
        self.boot_id = identity("boot_")
        self.updates = f"oe.internal.v1.vcp.runtimes.{binding['runtime_id']}.updates"

    async def recover(self) -> list[dict[str, Any]]:
        """Fence interrupted work, then journal `runtime.ready`. Returns the evidence for each decision."""
        async with self.store.transaction() as tx:
            meta = await tx.runtime()
            if meta is None:
                if self.established:
                    raise RecoveryRefused(Refusal.EMPTY, target=self.binding)
                meta = {"target": self.binding, "journal_id": identity("journal_"), "sequence": 0}
            if meta["target"] != self.binding:
                raise RecoveryRefused(Refusal.REBOUND, target=self.binding, journal_id=meta["journal_id"])
            meta["ready"] = False
            await tx.save_runtime(meta)
            attempts = await tx.attempts()
        evidence = []
        for attempt in attempts:
            command = attempt["command"]
            item = {"command_id": command["command_id"], "attempt_id": command["target"]["attempt_id"],
                    "runtime_generation": command["target"]["runtime_generation"], "journal_id": meta["journal_id"],
                    "journal_sequence": meta["sequence"], "state": attempt["state"], "launch_count": attempt["launch_count"]}
            if attempt["state"] in INTERRUPTED:
                # The launch may have happened: never launch again. Fencing may wait
                # on the executor, so no transaction is held across it.
                executor = self.executors.get(command["payload"]["agent"]["runner"])
                if executor is None or not await executor.fence(attempt):
                    raise RecoveryRefused(Refusal.UNFENCED, **item, decision="unresolved")
                async with self.store.transaction() as tx:
                    await self.finish(tx, await tx.attempt(item["attempt_id"]), "failed", {"error": {
                        "code": "PROVIDER_ERROR", "message": "Interrupted executor was fenced after daemon restart",
                        "retryable": False}, "reason": "executor_lost"}, "killed")
                item["decision"] = "fenced_cancelled" if attempt["cancel"] else "fenced_failed"
            elif attempt["state"] == "recorded":
                item["decision"] = "launch_once"
            else:
                item["decision"] = "replay_only"
            evidence.append(item)
        async with self.store.transaction() as tx:
            meta = await tx.runtime()
            if meta["blocked"]:
                evidence.append({"journal_id": meta["journal_id"], "decision": "blocked", **meta["blocked"]})
            sequence = await self.append(tx, "runtime.ready", {
                "executors": sorted(self.executors),
                "steering_executors": [name for name, executor in sorted(self.executors.items())
                                       if name == "fake" and getattr(executor, "steering", False)], "max_concurrent_attempts": self.max_concurrent_attempts,
                "progress_kinds": sorted({kind for e in self.executors.values() for kind in e.progress_kinds}),
                "journal_id": meta["journal_id"]})
            meta = await tx.runtime()
            meta["ready_sequence"] = sequence
            await tx.save_runtime(meta)
        for item in evidence:
            log.info("recovery decision", extra={f"daemon.{key}": value for key, value in item.items()})
        return evidence

    async def append(self, tx, kind: str, payload: dict[str, Any], attempt: dict[str, Any] | None = None,
                     termination: str | None = None, cancel: dict[str, Any] | None = None) -> int:
        meta = await tx.runtime()
        meta["sequence"] += 1
        message = {"event_id": identity("rte_"), "kind": kind, "target": attempt["command"]["target"] if attempt else self.binding,
                   "boot_id": self.boot_id, "sequence": meta["sequence"], "occurred_at": now(), "payload": payload}
        if attempt is not None:
            attempt["attempt_sequence"] += 1
            attempt["last_event_sequence"] = meta["sequence"]
            message.update(execution=attempt["command"]["execution"], attempt_sequence=attempt["attempt_sequence"])
        if termination is not None:
            message["termination"] = termination
        if cancel is not None:
            message.update(cancel_command_id=cancel["command_id"], cause=cancel["cause"])
        wire = encode(message)
        if errors := validate_wire_message("update", wire, self.updates):
            raise UnsendableUpdate(errors)
        await tx.append_update(meta["sequence"], message["event_id"], wire,
                               attempt and attempt["command"]["target"]["attempt_id"])
        await tx.save_runtime(meta)
        return meta["sequence"]

    async def finish(self, tx, attempt: dict[str, Any], state: str, payload: dict[str, Any], termination: str) -> None:
        if attempt["state"] in TERMINAL:
            return
        if cancel := attempt["cancel"]:
            # Cancellation was journaled first, so it decides the outcome.
            state, payload = "cancelled", cancel["payload"]
        if state == "completed" and attempt["command"]["payload"]["agent"]["runner"] == "fake":
            messages = (attempt.get("executor_handle") or {}).get("steering", [])
            if messages:
                payload = {**payload, "result": {**payload["result"], "summary": payload["result"]["summary"]
                           + "\n" + "\n".join(item["message"] for item in messages)}}
                summary = payload["result"]["summary"]
                if len(summary) > 65536:
                    suffix = "\n[steering sha256:" + digest(messages) + "]"
                    payload["result"]["summary"] = summary[:65536 - len(suffix)] + suffix
        attempt["state"] = state
        await self.append(tx, "attempt." + state, payload, attempt, termination, cancel)
        await tx.save_attempt(attempt)  # leaving the active states frees its session and capacity

    async def progress(self, attempt_id: str, payload: dict[str, Any]) -> None:
        """Journal one progress record for a running attempt, waiting while the outbox is full."""
        while True:
            async with self.store.read() as tx:
                if await tx.pending_updates() < self.max_pending_updates:
                    break
            await asyncio.sleep(self.backpressure_seconds)
        async with self.store.transaction() as tx:
            attempt = await tx.attempt(attempt_id)
            executor = self.executors[attempt["command"]["payload"]["agent"]["runner"]]
            if attempt["state"] not in ("running", "cancelling") or payload["kind"] not in executor.progress_kinds:
                raise ValueError(payload["kind"])  # only for a running attempt, of an advertised kind
            await self.append(tx, "attempt.progress", payload, attempt)
            await tx.save_attempt(attempt)

    async def execute(self, subject: str, command: dict[str, Any]) -> dict[str, Any]:
        response = {"command_id": command["command_id"], "target": command["target"], "status": "accepted"}
        if detail := mismatch(self.binding, subject, command, "execute"):
            return {**response, "status": "rejected", "error": detail}
        fingerprint = digest([subject, command])
        async with self.store.transaction() as tx:
            if previous := await tx.receipt(command["command_id"]):
                if previous["digest"] == fingerprint:
                    return previous["response"]
                return {**response, "status": "rejected", "error": error("COMMAND_ID_CONFLICT", "command identity changed")}
            tombstone = await tx.attempt(command["target"]["attempt_id"])
            if (tombstone and tombstone["command"]["kind"] == "cancel" and tombstone["command"]["target"] == command["target"]
                    and tombstone["command"]["execution"] == command["execution"]):
                # Cancelled before it arrived: acknowledged, but never launched or opened.
                await tx.remember(command, fingerprint, response)
                return response
            meta = await tx.runtime()
            if not meta["ready"] or meta["blocked"]:
                return {**response, "status": "rejected", "error": error("UNAVAILABLE", "runtime readiness is not committed")}
            target, payload = command["target"], command["payload"]
            session = await tx.session(target["session_id"])
            executor = self.executors.get(payload["agent"]["runner"])
            detail = None
            if await tx.attempt(target["attempt_id"]):
                detail = error("ATTEMPT_CONFLICT", "attempt is owned by another execute command")
            elif executor is None:
                detail = error("UNSUPPORTED_EXECUTOR", "executor is not configured")
            elif session and session.get("active_attempt"):
                detail = error("SESSION_BUSY", "session has an active attempt")
            elif (payload["session_mode"] == "continue") != (session is not None) or (session and session["agent"] != payload["agent"]):
                detail = error("INVALID_ARGUMENT", "session mapping does not match this execution")
            elif session and executor.conversational and not (
                    session["executor_session_id"] and session["executor_resumable"]):
                # Never replace a conversation that cannot be loaded with a new one.
                detail = error("INVALID_ARGUMENT", "the session's executor conversation cannot be resumed")
            else:
                try:
                    task_context.resolve(self.root, payload["context"])
                    executor.validate(payload)
                except (OSError, ValueError, RuntimeError):
                    detail = error("INVALID_ARGUMENT", "invalid or unavailable executor context")
            # Counted through a partial index over active attempts only.
            if detail is None and await tx.active_attempts() >= self.max_concurrent_attempts:
                return {**response, "status": "rejected", "error": error("UNAVAILABLE", "runtime is at capacity")}
            if detail is not None:
                response.update(status="rejected", error=detail)
            # Accepted and terminal rejections are remembered; UNAVAILABLE never is.
            await tx.remember(command, fingerprint, response)
            if detail is None:
                if session is None:
                    await tx.open_session(target["session_id"], command["command_id"])
                await tx.save_attempt({"command": command, "state": "recorded", "launch_count": 0,
                                       "attempt_sequence": 0, "last_event_sequence": 0, "cancel": None})
        return response

    async def cancel(self, subject: str, command: dict[str, Any]) -> dict[str, Any]:
        """Journal one cancel intent; never waits for the executor, and never refused for readiness."""
        response = {"command_id": command["command_id"], "target": command["target"], "status": "accepted"}
        if detail := mismatch(self.binding, subject, command, "cancel"):
            return {**response, "status": "rejected", "error": detail}
        fingerprint = digest([subject, command])
        target = command["target"]
        async with self.store.transaction() as tx:
            if previous := await tx.receipt(command["command_id"]):
                if previous["digest"] == fingerprint:
                    return previous["response"]
                return {**response, "status": "rejected", "error": error("COMMAND_ID_CONFLICT", "command identity changed")}
            execute = await tx.receipt(command["execution"]["command_id"])
            attempt = await tx.attempt(target["attempt_id"])
            if ((execute and execute["response"]["target"] != target) or (attempt and (
                    attempt["command"]["target"] != target or attempt["command"]["execution"] != command["execution"]))):
                response.update(status="rejected", error=error("ATTEMPT_CONFLICT", "cancel names another execution"))
            await tx.remember(command, fingerprint, response)
            if response["status"] == "rejected" or (execute and execute["response"]["status"] == "rejected"):
                # A rejected execute launched nothing; VCP already settles it from that rejection.
                return response
            if attempt is None:
                # Cancel before execute: a tombstone for this exact binding, cancelled at once.
                attempt = {"command": command, "state": "recorded", "launch_count": 0, "attempt_sequence": 0,
                           "last_event_sequence": 0, "cancel": None}
            if attempt["state"] in TERMINAL or attempt["cancel"]:
                return response  # it keeps its outcome, or the first cancel already decides it
            attempt["cancel"] = command
            await tx.save_attempt(attempt)  # its updates reference the attempt row
            if attempt["state"] == "recorded":
                await self.finish(tx, attempt, "cancelled", command["payload"], "never_started")
            else:
                attempt["state"] = "cancelling"  # ownership is kept until the worker confirms termination
                await tx.save_attempt(attempt)
        return response

    async def steer(self, subject: str, command: dict[str, Any]) -> dict[str, Any]:
        """Apply only to the exact running fake attempt, in the receipt/update transaction."""
        response = {"command_id": command["command_id"], "target": command["target"], "status": "accepted"}
        if detail := mismatch(self.binding, subject, command, "steer"):
            return {**response, "status": "rejected", "error": detail}
        fingerprint = digest([subject, command])
        async with self.store.transaction() as tx:
            if previous := await tx.receipt(command["command_id"]):
                if previous["digest"] == fingerprint:
                    return previous["response"]
                return {**response, "status": "rejected", "error": error("COMMAND_ID_CONFLICT", "command identity changed")}
            attempt = await tx.attempt(command["target"]["attempt_id"])
            detail = None
            if (not attempt or attempt["command"]["target"] != command["target"]
                    or attempt["command"]["execution"] != command["execution"]):
                detail = error("ATTEMPT_CONFLICT", "steering names another execution")
            else:
                runner = attempt["command"]["payload"]["agent"]["runner"]
                executor = self.executors.get(runner)
                if runner != "fake" or not getattr(executor, "steering", False):
                    detail = error("UNSUPPORTED_CAPABILITY", "executor does not support steering")
            if detail:
                response.update(status="rejected", error=detail)
            await tx.remember(command, fingerprint, response)
            if detail:
                return response
            payload = {"command_id": command["command_id"], "cause": command["cause"]}
            if attempt["state"] != "running":
                payload["error"] = {"code": "INVALID_STATE", "message": "attempt is no longer running", "retryable": False,
                                    "details": {"resource_type": "attempt", "resource_id": command["target"]["attempt_id"],
                                                "current_state": attempt["state"], "allowed_states": ["running"]}}
                outcome = "failed"
            else:
                executor.apply_steering(attempt["executor_handle"], command)
                outcome = "applied"
            await self.append(tx, "steering." + outcome, payload, attempt)
            await tx.save_attempt(attempt)
        return response

    async def inspect(self, subject: str, request: dict[str, Any]) -> dict[str, Any]:
        response = {"request_id": request["request_id"], "target": request["target"], "status": "ok"}
        if detail := mismatch(self.binding, subject, request, "inspect"):
            return {**response, "status": "error", "error": detail}
        async with self.store.read() as tx:
            meta = await tx.runtime()
            snapshots = []
            for attempt_id in request["attempt_ids"]:
                attempt = await tx.attempt(attempt_id)
                snapshot = {"attempt_id": attempt_id, "known": attempt is not None}
                if attempt is not None:
                    snapshot.update(target=attempt["command"]["target"], execution=attempt["command"]["execution"],
                                    state=attempt["state"], last_event_sequence=attempt["last_event_sequence"])
                snapshots.append(snapshot)
        response["result"] = {"boot_id": self.boot_id, "ready": meta["ready"] and not meta["blocked"],
                              "last_event_sequence": meta["sequence"], "attempts": snapshots}
        return response
