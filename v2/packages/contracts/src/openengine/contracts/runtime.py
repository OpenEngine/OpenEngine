"""Stateless checks for the internal runtime contract; no transport implementation."""

from __future__ import annotations

import json
from functools import cache
from math import isfinite

from openengine.contracts.validate import CONTRACTS, _validator


ROOT = CONTRACTS
CONTRACT = ROOT / "internal/workspace_runtime/v1"
MAX_WIRE_BYTES = 1024 * 1024
DEFINITIONS = {
    "execute": ("runtime", ("execute",)),
    "cancel": ("runtime", ("cancel",)),
    "command_ack": ("runtime", ("execute", "cancel")),
    "inspect": ("runtime", ("inspect",)),
    "inspect_response": ("runtime", ("inspect",)),
    **{
        name: ("vcp", ("updates",))
        for name in (
            "update",
            "ready",
            "started",
            "progress",
            "completed",
            "failed",
            "cancelled",
            "update_ack",
        )
    },
}


@cache
def validator(definition):
    if definition not in DEFINITIONS:
        raise ValueError(definition)
    return _validator(
        (CONTRACT / "messages.schema.json").as_uri() + "#/$defs/" + definition
    )


def _reject_constant(value):
    raise ValueError(value)


def _finite_float(value):
    number = float(value)
    if not isfinite(number):
        raise ValueError(value)
    return number


def _timestamp_key(value):
    """Order schema-validated UTC timestamps without losing nanoseconds."""
    seconds, _, fraction = value.removesuffix("Z").partition(".")
    return seconds, fraction.ljust(9, "0")


def validate_wire_message(definition, data: bytes, subject):
    """Validate received NATS payload bytes before decoding or parsing JSON."""
    if len(data) > MAX_WIRE_BYTES:
        return ["Message exceeds 1 MiB"]
    try:
        payload = json.loads(
            data.decode("utf-8"),
            parse_constant=_reject_constant,
            parse_float=_finite_float,
        )
    except (ValueError, RecursionError):
        return ["Message must be valid UTF-8 JSON"]
    return validate_message(definition, payload, subject)


def validate_message(definition, payload, subject):
    """Check a decoded body; transport callers must use validate_wire_message."""
    # Stop at the first error: maxItems rejects oversized arrays before
    # uniqueItems can perform quadratic comparisons on their contents.
    try:
        error = next(validator(definition).iter_errors(payload), None)
        if error is not None:
            return [error.message]
    except RecursionError:
        return ["Message exceeds validation nesting limits"]
    errors = []
    service, actions = DEFINITIONS[definition]
    expected = [
        f"oe.internal.v1.{service}.runtimes.{payload['target']['runtime_id']}.{action}"
        for action in actions
    ]
    if subject not in expected:
        errors.append("Subject does not match message kind and runtime identity")
    for cause in [payload.get("cause"), payload.get("execution", {}).get("cause")]:
        if cause and cause["causal_path"][-1] != cause["operation"]["command_id"]:
            errors.append("Cause path must end with its public command ID")
    if (
        definition == "execute"
        and payload["execution"]["command_id"] != payload["command_id"]
    ):
        errors.append("Execute identity must equal its command ID")
    if (
        definition == "cancel"
        and payload["execution"]["command_id"] == payload["command_id"]
    ):
        errors.append("Control command needs a distinct command ID")
    if payload.get("kind") == "attempt.progress":
        progress = payload["payload"]
        if progress["kind"] == "artifact":
            for key in ("workspace_id", "attempt_id"):
                if progress["artifact"].get(key) != payload["target"][key]:
                    errors.append(f"Artifact {key} differs from attempt binding")
        if progress["kind"] == "usage" and _timestamp_key(
            progress["period_start"]
        ) > _timestamp_key(progress["period_end"]):
            errors.append("Usage period ends before it starts")
    if (
        definition == "update_ack"
        and payload.get("expected_sequence", 0) >= payload["sequence"]
    ):
        errors.append("Gap must request an earlier sequence")
    if definition == "execute":
        context = payload["payload"]["context"]
        for value in [context["working_directory"], *context["instruction_files"]]:
            if (
                value.startswith("/")
                or ".." in value.split("/")
                or "\\" in value
                or "\x00" in value
                or ":" in value
            ):
                errors.append("Context paths must stay relative to the workspace")
    if definition == "inspect_response" and payload["status"] == "ok":
        result = payload["result"]
        seen = set()
        for snapshot in result["attempts"]:
            if snapshot["attempt_id"] in seen:
                errors.append("Duplicate attempt snapshot")
            seen.add(snapshot["attempt_id"])
            if snapshot["known"]:
                if snapshot["attempt_id"] != snapshot["target"]["attempt_id"]:
                    errors.append("Snapshot attempt identity mismatch")
                if binding_errors(snapshot, payload["target"]):
                    errors.append("Snapshot runtime binding mismatch")
                cause = snapshot["execution"]["cause"]
                if cause["causal_path"][-1] != cause["operation"]["command_id"]:
                    errors.append(
                        "Snapshot cause path must end with its public command ID"
                    )
                if snapshot["last_event_sequence"] > result["last_event_sequence"]:
                    errors.append("Snapshot exceeds journal high-water sequence")
    return errors


def binding_errors(payload, provisioned_binding):
    """Compare wire claims to independently provisioned ownership, never authenticate them."""
    return [
        f"Binding mismatch: {key}"
        for key, value in provisioned_binding.items()
        if payload["target"].get(key) != value
    ]


def check():
    errors = []
    for path in sorted((CONTRACT / "examples").glob("*.json")):
        case = json.loads(path.read_text())
        errors.extend(
            f"{path.name}: {error}"
            for error in validate_wire_message(
                case["definition"],
                json.dumps(case["payload"], ensure_ascii=False).encode("utf-8"),
                case.get("subject", case.get("in_reply_to")),
            )
        )
    return errors


def validate_exchange(request_definition, request, reply, subject):
    """Check a native reply against its request, including echoed identities."""
    reply_definition = {
        "execute": "command_ack",
        "cancel": "command_ack",
        "inspect": "inspect_response",
        "update": "update_ack",
    }.get(request_definition)
    if reply_definition is None:
        raise ValueError(request_definition)
    errors = validate_message(request_definition, request, subject)
    errors += validate_message(reply_definition, reply, subject)
    if errors:
        return errors
    identity = (
        "request_id"
        if request_definition == "inspect"
        else "event_id"
        if request_definition == "update"
        else "command_id"
    )
    if request[identity] != reply[identity]:
        errors.append("Reply identity differs from request")
    expected = (
        request["target"]
        if request_definition != "update"
        else {
            key: request["target"][key]
            for key in ("workspace_id", "runtime_id", "runtime_generation")
        }
    )
    if reply["target"] != expected:
        errors.append("Reply binding differs from request")
    if request_definition == "update" and request["sequence"] != reply["sequence"]:
        errors.append("Reply sequence differs from update")
    if request_definition == "inspect" and reply["status"] == "ok":
        if set(request["attempt_ids"]) != {
            snapshot["attempt_id"] for snapshot in reply["result"]["attempts"]
        }:
            errors.append("Inspect must return exactly the requested attempts")
    return errors


if __name__ == "__main__":
    problems = check()
    print(
        "\n".join(problems)
        if problems
        else "Internal workspace runtime contract examples valid"
    )
    raise SystemExit(bool(problems))
