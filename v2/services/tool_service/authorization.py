"""Cedar authorization for tool invocations.

A grant and the operator's ceiling policies are Cedar policies, and a call is
allowed only if some grant permits it and no ceiling forbids it. A grant is the
`permit` for one subject to invoke one tool, narrowed by its `when` condition
and its expiry; a ceiling is a `forbid` a grant may never exceed. The two are
evaluated together, so a ceiling always wins.

The grant's `when` is checked when the grant is applied (it must parse,
reference only `context` and `resource`, and type-check against the tool's
`inputSchema`). A ceiling is checked when it is applied too, but only that it
parses and is a `forbid`: it is global, so it is not type-checked against one
tool. Neither is re-checked when a call is decided; the compiled policies are
simply evaluated, and a reference to an argument the called tool does not take
is inert at decision time.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from cedar import (
    Authorizer,
    Context,
    Entity,
    EntityUid,
    Policy,
    PolicySet,
    Request,
    Schema,
)

INVOKE = "invoke"
"""The single Cedar action every tool call is authorized against."""

_PRINCIPAL = "StepAttempt"
_WORKSPACE = "Workspace"
_TOOL = "Tool"
_ACTION = "Action"


class CedarError(ValueError):
    """A grant's `when` or a ceiling policy does not parse or type-check."""

    def __init__(self, field: str, problem: str) -> None:
        super().__init__(f"{field}: {problem}")


@dataclass(frozen=True)
class Grant:
    """One row of `tool_grants`, as Cedar needs it."""

    uid: str
    name: str
    workspace: str
    step_attempt_id: str | None
    when: str | None
    expires_at: datetime | None


@dataclass(frozen=True)
class Ceiling:
    """One row of `policies`: a Cedar `forbid` an operator applied."""

    uid: str
    policy: str


@dataclass(frozen=True)
class GrantRef:
    """A grant that allowed a call, as `ToolInvocation.status.authorization` stores it."""

    name: str
    uid: str


@dataclass(frozen=True)
class Decision:
    """The outcome of authorizing one call."""

    allowed: bool
    grant_refs: tuple[GrantRef, ...]


def cedar_type(json_schema: Mapping[str, Any]) -> dict[str, Any] | None:
    """Translate one JSON Schema fragment into the Cedar type of its values.

    Returns ``None`` for a type Cedar's schema parser does not take (notably
    arrays), so the caller can leave it out of the context.
    """
    if not isinstance(json_schema, Mapping):
        return None
    shape = json_schema.get("type")
    if shape == "object" or (shape is None and "properties" in json_schema):
        attributes: dict[str, Any] = {}
        for name, sub in (json_schema.get("properties") or {}).items():
            member = cedar_type(sub)
            if member is not None:
                attributes[name] = member
        return {"type": "Record", "attributes": attributes}
    if shape == "string":
        return {"type": "String"}
    if shape in ("integer", "number"):
        # Cedar's schema parser takes no Decimal; a number is a Long.
        return {"type": "Long"}
    if shape == "boolean":
        return {"type": "Boolean"}
    return None


def schema_for_tool(tool_spec: Mapping[str, Any]) -> Schema:
    """The Cedar schema that a tool's grants and ceilings must type-check against.

    Its single action's context is the call: `now`, the time the call is
    decided, in epoch milliseconds, and `arguments`, the tool's input.
    """
    arguments = cedar_type(tool_spec.get("inputSchema") or {}) or {"type": "Record", "attributes": {}}
    document = {
        "": {
            "entityTypes": {_PRINCIPAL: {}, _WORKSPACE: {}, _TOOL: {}},
            "actions": {
                INVOKE: {
                    "appliesTo": {
                        "principalTypes": [_PRINCIPAL, _WORKSPACE],
                        "resourceTypes": [_TOOL],
                        "context": {
                            "type": "Record",
                            "attributes": {"now": {"type": "Long"}, "arguments": arguments},
                        },
                    }
                }
            },
        }
    }
    return Schema.from_json(json.dumps(document))


def _type_check(policies: PolicySet, tool_spec: Mapping[str, Any], what: str) -> None:
    result = schema_for_tool(tool_spec).validate_policyset(policies)
    if not result.valid:
        detail = "; ".join(error.message for error in result.errors)
        raise CedarError(what, f"does not type-check: {detail}")


_FORBIDDEN = frozenset({"principal", "action"})
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _identifiers(condition: str) -> set[str]:
    """The identifiers a Cedar condition names, skipping string literals and comments."""
    found: set[str] = set()
    i, length = 0, len(condition)
    while i < length:
        char = condition[i]
        if char in "\"'":
            while i < length and condition[i] != char:
                i += 2 if condition[i] == "\\" else 1
            i += 1
        elif char == "/" and condition[i : i + 2] == "//":
            while i < length and condition[i] != "\n":
                i += 1
        elif char == "/" and condition[i : i + 2] == "/*":
            i += 2
            while i + 1 < length and condition[i : i + 2] != "*/":
                i += 1
            i += 2
        else:
            match = _IDENT.match(condition, i)
            if match:
                found.add(match.group(0))
                i = match.end()
            else:
                i += 1
    return found


def validate_condition(condition: str | None, tool_spec: Mapping[str, Any]) -> None:
    """Check a grant's `when` when the grant is applied.

    Raises `CedarError` when it is empty and not omitted, references `principal`
    or `action`, does not parse, or does not type-check against the tool's
    `inputSchema`.
    """
    if not condition:
        return
    forbidden = _FORBIDDEN & _identifiers(condition)
    if forbidden:
        named = ", ".join(f"`{name}`" for name in sorted(forbidden))
        raise CedarError("spec.when", f"may reference only `context` and `resource`, not {named}")
    wrapped = (
        f'permit(principal, action == Action::"{INVOKE}", resource == Tool::"dummy") '
        f"when {{ {condition} }};"
    )
    try:
        policies = PolicySet()
        policies.add(Policy(wrapped, id="when"))
    except ValueError as exc:
        raise CedarError("spec.when", f"does not parse: {exc}") from exc
    _type_check(policies, tool_spec, "spec.when")


def _first_token(policy: str) -> str:
    """The first identifier in a Cedar statement, skipping whitespace and comments."""
    i, length = 0, len(policy)
    while i < length:
        if policy[i].isspace():
            i += 1
        elif policy[i : i + 2] == "//":
            while i < length and policy[i] != "\n":
                i += 1
        elif policy[i : i + 2] == "/*":
            i += 2
            while i + 1 < length and policy[i : i + 2] != "*/":
                i += 1
            i += 2
        else:
            break
    match = _IDENT.match(policy, i)
    return match.group(0) if match else ""


def validate_ceiling(policy: str | None) -> None:
    """Check a ceiling's Cedar text when it is applied.

    A ceiling is operator-written and global, so it is not type-checked against
    one tool's `inputSchema` (it may name any tool's argument pattern). It must
    parse, and it must be a `forbid`: a ceiling only restricts, and Cedar's
    forbid-overrides-permit is what makes it win over grants. A reference to an
    argument the called tool does not take is simply inert at decision time.

    Raises `CedarError` when it does not parse or is not a `forbid`.
    """
    if not policy:
        return
    try:
        policies = PolicySet()
        policies.add(Policy(policy, id="ceiling"))
    except ValueError as exc:
        raise CedarError("spec.policy", f"does not parse: {exc}") from exc
    if _first_token(policy) != "forbid":
        raise CedarError("spec.policy", "must be a `forbid`; a ceiling only restricts")


def _principal_of(grant: Grant) -> EntityUid:
    if grant.step_attempt_id is not None:
        return EntityUid(_PRINCIPAL, grant.step_attempt_id)
    return EntityUid(_WORKSPACE, grant.workspace)


def compile_grant_policy(grant: Grant, tool_uid: str) -> Policy:
    """The `permit` a grant becomes: one subject, one tool, its condition and expiry."""
    clauses = [grant.when] if grant.when else []
    if grant.expires_at is not None:
        clauses.append(f"context.now < {int(grant.expires_at.timestamp() * 1000)}")
    when = f" when {{ {' && '.join(clauses)} }}" if clauses else ""
    text = (
        f"permit(\n"
        f"  principal == {_principal_of(grant)},\n"
        f'  action == Action::"{INVOKE}",\n'
        f'  resource == Tool::"{tool_uid}"\n'
        f"){when};"
    )
    return Policy(text, id=grant.uid)


def compile_ceiling_policy(ceiling: Ceiling) -> Policy:
    """A ceiling as a policy, so its `uid` is what a denial names in `reason`."""
    return Policy(ceiling.policy, id=ceiling.uid)


def decide(
    *,
    tool_uid: str,
    grants: list[Grant],
    ceilings: list[Ceiling],
    principal_kind: str,
    principal_id: str,
    workspace: str,
    arguments: Mapping[str, Any],
    now_ms: int,
) -> Decision:
    """Authorize one call: allowed when a grant permits and no ceiling forbids.

    `principal_kind` is `StepAttempt` or `Workspace`, the caller the verified
    credentials named. `grant_refs` names the grants whose permit fired, so the
    call's record shows exactly what allowed it.
    """
    authorizer = Authorizer()
    authorizer.upsert_entity(Entity(uid=EntityUid(_ACTION, INVOKE), attrs={}))
    authorizer.upsert_entity(Entity(uid=EntityUid(_TOOL, tool_uid), attrs={}))
    authorizer.upsert_entity(Entity(uid=EntityUid(_WORKSPACE, workspace), attrs={}))
    if principal_kind == _PRINCIPAL:
        authorizer.upsert_entity(
            Entity(uid=EntityUid(_PRINCIPAL, principal_id), attrs={}, parents=[EntityUid(_WORKSPACE, workspace)])
        )

    policies = PolicySet()
    for grant in grants:
        policies.add(compile_grant_policy(grant, tool_uid))
    for ceiling in ceilings:
        policies.add(compile_ceiling_policy(ceiling))

    request = Request(
        principal=EntityUid(principal_kind, principal_id),
        action=EntityUid(_ACTION, INVOKE),
        resource=EntityUid(_TOOL, tool_uid),
        context=Context({"now": now_ms, "arguments": dict(arguments)}),
    )
    response = authorizer.is_authorized(request, policies)

    grants_by_uid = {grant.uid: grant for grant in grants}
    refs = tuple(
        GrantRef(name=grants_by_uid[uid].name, uid=uid) for uid in response.reason if uid in grants_by_uid
    )
    return Decision(allowed=response.allowed, grant_refs=refs)
