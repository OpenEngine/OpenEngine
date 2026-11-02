"""Deliver accepted internal commands and project ordered runtime updates.

Internal subjects are not public requests: they never reach the shared Server
or its Authorizer, and public policy never admits or rejects them. Runtime
identity and generation are checked against VCP's own ownership registry
(`workspace_runtimes` and the workspace snapshot) instead. Public execution
was already authorized when its command bytes were accepted.

The dispatcher sends each accepted command's exact stored bytes until the
daemon durably acknowledges it, retrying unchanged with capped, jittered
backoff. The update handler commits update identity, the contiguous runtime
sequence and the resulting attempt, session, operation and public event
changes in one transaction before replying `committed`. Neither knows where
the runtime is placed: a subject names only the runtime ID.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from openengine.contracts.runtime import digest, error, validate_exchange, validate_wire_message
from openengine.messaging import ServiceError, envelopes
from openengine.messaging.retry import Backoff

from virtual_control_plane.application.execution import Execution, InvalidTransition
from virtual_control_plane.application.lifecycle import identity
from virtual_control_plane.application.ports import RuntimeTransport
from virtual_control_plane.application.transactions import RuntimeOutbox

log = logging.getLogger(__name__)

BINDING = ("workspace_id", "runtime_id", "runtime_generation")
# Durable execute rejections establishing that the daemon recorded and launched nothing.
NOT_LAUNCHED = frozenset({"INVALID_ARGUMENT", "UNSUPPORTED_EXECUTOR", "SESSION_BUSY"})
STATES = {"attempt.started": "running", "attempt.completed": "completed", "attempt.failed": "failed"}


class Rejected(Exception):
    def __init__(self, code: str, message: str, *, expected: int | None = None, quarantine: bool = False) -> None:
        super().__init__(f"{code}: {message}")
        self.code, self.message, self.expected, self.quarantine = code, message, expected, quarantine

    def error(self) -> dict[str, Any]:
        return error(self.code, self.message)


def fence(runtime: dict | None, workspace: dict | None, target: dict) -> Rejected | None:
    """The claimed runtime binding against VCP's ownership registry, before any dedup state."""
    if not runtime or not workspace or runtime["runtime_id"] != target["runtime_id"]:
        return Rejected("IDENTITY_MISMATCH", "runtime is not owned by this workspace")
    current = workspace.get("runtime", {})
    if (runtime["generation"] != target["runtime_generation"] or current.get("runtime_id") != target["runtime_id"]
            or current.get("generation") != target["runtime_generation"]):
        return Rejected("STALE_RUNTIME", "runtime generation is not current")
    return None


class RuntimeUpdates:
    def __init__(self, store, execution: Execution) -> None:
        self.store, self.execution = store, execution

    async def receive(self, subject: str, data: bytes) -> bytes | None:
        """The encoded update_ack, or None for a message too malformed to acknowledge."""
        if errors := validate_wire_message("update", data, subject):
            log.warning("discarded invalid runtime update", extra={"vcp.errors": errors[:3]})
            return None
        message = json.loads(data)
        target = {key: message["target"][key] for key in BINDING}
        reply = {"event_id": message["event_id"], "target": target, "sequence": message["sequence"], "status": "committed"}
        try:
            async with self.store.transaction() as tx:
                await self._commit(tx, message)
        except Rejected as rejection:
            reply.update(status="rejected", error=rejection.error())
            if rejection.expected is not None:
                reply["expected_sequence"] = rejection.expected
            if rejection.quarantine:
                await self._quarantine(message, rejection)
            log.info("runtime update rejected", extra={"vcp.runtime_id": target["runtime_id"],
                     "vcp.runtime_generation": target["runtime_generation"], "vcp.event_id": message["event_id"],
                     "vcp.sequence": message["sequence"], "vcp.code": rejection.code})
        return envelopes.encode(reply)

    async def _commit(self, tx, message: dict[str, Any]) -> None:
        target, kind = message["target"], message["kind"]
        runtime = await tx.get("workspace_runtimes", target["workspace_id"])
        workspace = ((await tx.get("workspaces", target["workspace_id"])) or {}).get("snapshot")
        if (kind == "runtime.ready" and workspace and workspace["state"] == "provisioning" and runtime
                and runtime["runtime_id"] == target["runtime_id"] and runtime["generation"] == target["runtime_generation"]):
            raise Rejected("UNAVAILABLE", "workspace creation has not committed yet")
        if rejection := fence(runtime, workspace, target):
            raise rejection
        assert workspace is not None
        if previous := await tx.runtime_update(target["runtime_id"], message["event_id"]):
            if previous != digest(message):
                raise Rejected("EVENT_CONFLICT", "event ID was reused with different content", quarantine=True)
            return  # Duplicate delivery: committed without repeating any effect.
        # The fenced runtime still owns its attempts whatever the workspace state,
        # so their updates project; only a new readiness claim waits for `ready`.
        if kind == "runtime.ready" and workspace["state"] != "ready":
            raise Rejected("UNAVAILABLE", "workspace is not ready")
        status = await tx.get("runtime_status", target["runtime_id"]) or {}
        if status.get("quarantined"):
            raise Rejected("EVENT_CONFLICT", "runtime requires reconciliation")
        expected = status.get("sequence", 0) + 1
        if message["sequence"] < expected:
            raise Rejected("EVENT_CONFLICT", "sequence is already occupied", quarantine=True)
        if message["sequence"] > expected:
            raise Rejected("EVENT_GAP", f"replay sequence {expected} first", expected=expected)
        if kind == "runtime.ready":
            payload = message["payload"]
            if status.get("journal_id") not in (None, payload["journal_id"]):
                raise Rejected("IDENTITY_MISMATCH", "runtime journal was replaced", quarantine=True)
            status.update(workspace_id=target["workspace_id"], generation=target["runtime_generation"], ready=True,
                          **{key: payload[key] for key in ("executors", "max_concurrent_attempts", "progress_kinds", "journal_id")})
        elif not status.get("journal_id"):
            raise Rejected("IDENTITY_MISMATCH", "runtime has not registered its journal")
        else:
            await self._project(tx, message, status.get("progress_kinds", []))
        status.update(sequence=message["sequence"], boot_id=message["boot_id"])
        await tx.put("runtime_status", target["runtime_id"], status)
        await tx.record_runtime_update(target["runtime_id"], message["event_id"], message["sequence"], digest(message))

    async def _project(self, tx, message: dict[str, Any], progress_kinds: list[str]) -> None:
        target = message["target"]
        attempt_id = target["attempt_id"]
        binding = await tx.get("attempt_bindings", attempt_id)
        if (not binding or any(binding[key] != value for key, value in target.items())
                or binding["execution"] != message["execution"]):
            raise Rejected("IDENTITY_MISMATCH", "unknown attempt or execution identity")
        record = await tx.get("attempts", attempt_id)
        sequence = record.get("attempt_sequence", 0) + 1
        if message["attempt_sequence"] != sequence:
            raise Rejected("EVENT_CONFLICT", "attempt sequence is not contiguous", quarantine=True)
        state, payload = STATES.get(message["kind"]), message["payload"]
        progress = message["kind"] == "attempt.progress" and payload["kind"] in progress_kinds
        if state is None and not progress:
            raise Rejected("EVENT_CONFLICT", "update kind was not registered by this runtime", quarantine=True)
        try:
            if progress:
                await self.execution.progress(tx, attempt_id, payload)
            else:
                await self.execution.transition(tx, attempt_id, state, error=payload.get("error"), result=payload.get("result"))
        except InvalidTransition:
            raise Rejected("EVENT_CONFLICT", "invalid attempt transition", quarantine=True) from None
        record = await tx.get("attempts", attempt_id)
        record["attempt_sequence"] = sequence
        await tx.put("attempts", attempt_id, record)

    async def _quarantine(self, message: dict[str, Any], rejection: Rejected) -> None:
        # A separate transaction: the rejected update's own writes rolled back.
        runtime_id = message["target"]["runtime_id"]
        async with self.store.transaction() as tx:
            if status := await tx.get("runtime_status", runtime_id):
                status.update(quarantined=True, ready=False, quarantine={
                    "code": rejection.code, "event_id": message["event_id"], "sequence": message["sequence"]})
                await tx.put("runtime_status", runtime_id, status)


class RuntimeCommands:
    """Delivers each accepted internal command until the daemon's durable acknowledgement."""

    def __init__(self, store: RuntimeOutbox, transport: RuntimeTransport, execution: Execution, *,
                 batch: int = 16, poll_seconds: float = 0.1) -> None:
        self.store, self.transport, self.execution = store, transport, execution
        self.batch, self.poll_seconds = batch, poll_seconds
        self.backoff = Backoff()
        self._worker: asyncio.Task | None = None

    async def run_once(self) -> int:
        """Deliver due pending commands; return how many reached a durable outcome.

        Commands still backing off are left out of the batch, and each runtime's
        commands go in order beside every other runtime's, so an unreachable
        runtime neither fills the batch nor delays the others' sends.
        """
        runtimes: dict[str, list] = {}
        for command in await self.store.pending_runtime_commands(self.batch, exclude=self.backoff.waiting()):
            runtimes.setdefault(command.subject, []).append(command)
        return sum(await asyncio.gather(*(self._deliver_all(commands) for commands in runtimes.values())))

    async def _deliver_all(self, commands: list) -> int:
        settled = 0
        for index, command in enumerate(commands):
            try:
                settled += await self._deliver(command)
            except ServiceError:
                # Unreachable: its later commands wait out the same backoff
                # instead of each waiting for its own timeout.
                for later in commands[index:]:
                    self.backoff.failed(later.command_id)
                break
        return settled

    async def _deliver(self, command) -> bool:
        """Whether the command settled; raises ServiceError when the runtime did not answer."""
        # A lost request or reply leaves the outcome unknown, so the same bytes are retried.
        reply = await self.transport.send(command.subject, command.wire)
        request = json.loads(command.wire)
        errors = validate_wire_message("command_ack", reply, command.subject)
        ack = {} if errors else json.loads(reply)
        errors = errors or validate_exchange(request["kind"], request, ack, command.subject)
        if errors:
            # An unverifiable reply is not acknowledgement.
            log.warning("unverifiable runtime acknowledgement", extra={"vcp.command_id": command.command_id})
            self.backoff.failed(command.command_id)
            return False
        if ack["status"] == "rejected" and ack["error"]["retryable"]:
            # UNAVAILABLE is not remembered by the daemon: retry unchanged.
            self.backoff.failed(command.command_id)
            return False
        status = "acknowledged" if ack["status"] == "accepted" else "blocked"
        async with self.store.transaction() as tx:
            if status == "blocked" and ack["error"]["code"] in NOT_LAUNCHED:
                try:
                    await self.execution.transition(tx, request["target"]["attempt_id"], "failed", error={
                        "code": "PROVIDER_ERROR", "message": ack["error"]["message"], "retryable": False})
                    status = "rejected"
                except InvalidTransition:
                    pass
            await tx.settle_runtime_command(command.command_id, status, reply)
        # Identity, generation and conflict rejections are not evidence that no
        # executor exists: the attempt stays active until reconciled.
        log.info("runtime command settled", extra={
            "vcp.command_id": command.command_id, "vcp.runtime_id": request["target"]["runtime_id"],
            "vcp.runtime_generation": request["target"]["runtime_generation"], "vcp.delivery": status,
            "vcp.code": ack.get("error", {}).get("code")})
        self.backoff.done(command.command_id)
        return True

    async def inspect(self, runtime: dict[str, Any], workspace_id: str, attempt_ids: list[str]) -> dict[str, Any]:
        """Read-only reconciliation evidence from the runtime's journal; never launches or cancels."""
        target = {"workspace_id": workspace_id, "runtime_id": runtime["runtime_id"], "runtime_generation": runtime["generation"]}
        request = {"request_id": identity("inspect_"), "target": target, "attempt_ids": attempt_ids}
        subject = f"oe.internal.v1.runtime.runtimes.{runtime['runtime_id']}.inspect"
        reply = await self.transport.send(subject, envelopes.encode(request))
        errors = validate_wire_message("inspect_response", reply, subject)
        response = {} if errors else json.loads(reply)
        if errors or validate_exchange("inspect", request, response, subject):
            raise ServiceError("UNAVAILABLE", "unverifiable inspect response")
        return response

    def start(self) -> None:
        self._worker = asyncio.create_task(self._run())

    async def _run(self) -> None:
        while True:
            try:
                await self.run_once()
            except Exception:
                log.exception("runtime command delivery failed; retrying")
            await asyncio.sleep(self.poll_seconds)

    async def close(self) -> None:
        if self._worker:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
            self._worker = None
