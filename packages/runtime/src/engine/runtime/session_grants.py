"""What "allow this for the session" is allowed to mean next time.

A session grant is reuse of a decision, which is the only kind of permission
that can surprise the person who gave it: nobody is shown the second request,
so whatever the match rule lets through is what they will be taken to have
agreed to. So the rule here is deliberately the narrowest one that is still
useful -- the *same* action, in the same conversation, on the same runner, in
the same worktree -- and every axis is compared for equality rather than
contained, prefixed, or globbed.

Normalization exists because "the same action" is not the same string twice.
Providers re-render what they ask about: whitespace moves, a path arrives
relative one turn and absolute the next. So each request is reduced to one
canonical line, and two requests match when their lines are equal. What cannot
be reduced -- a command we were never told, a file change with no path in it --
normalizes to `None` and therefore matches nothing, which asks the user again.
That is the safe direction to fail in, and the only one.

The domain deliberately owns no JSON codec, so this lives in the runtime: a
provider's arguments arrive as text, and reading a path out of them is parsing.
"""

import json
import os
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from engine.domain.approvals import (
    ApprovalDecision,
    ApprovalKind,
    ApprovalRecord,
    SessionGrant,
)
from engine.domain.ids import SessionGrantId

#: Where a file-changing tool keeps the one path it is about. Claude's `Write`
#: and `Edit` use `file_path`; `NotebookEdit` uses `notebook_path`.
PATH_FIELDS = ("file_path", "path", "notebook_path")

#: Where a request keeps several paths at once. Codex describes a file change as
#: a map of path to what is being done to it, so its keys are the paths.
PATH_COLLECTION_FIELDS = ("paths", "file_paths", "changes", "file_changes")


def normalized_scope(record: ApprovalRecord) -> str | None:
    """The one thing this request is asking to be allowed, canonically.

    `None` means "not reducible to one thing", which is not a failure: it is a
    request that will always be put to the user, because we cannot say what
    reusing consent for it would cover.
    """
    match record.kind:
        case ApprovalKind.COMMAND_EXECUTION:
            command = _collapsed(record.command)
            if not command:
                return None
            return f"command_execution|{_directory(record.cwd)}|{command}"
        case ApprovalKind.FILE_CHANGE:
            paths = _paths_in(record)
            if not paths:
                return None
            return f"file_change|{','.join(paths)}"
        case ApprovalKind.TOOL_USE:
            tool_name = _collapsed(record.tool_name)
            if not tool_name:
                return None
            return f"tool_use|{tool_name}|{_canonical_arguments(record.arguments)}"
    return None


def session_grant_from(record: ApprovalRecord) -> SessionGrant | None:
    """The reusable consent an `accept_for_session` on this request creates.

    `None` when there is nothing narrow enough to reuse -- the decision still
    stands and is still sent to the provider, which will honour it for the rest
    of its own process. It simply leaves us nothing to match a later request
    against, so the next turn asks again.
    """
    scope = normalized_scope(record)
    if scope is None:
        return None
    if ApprovalDecision.ACCEPT_FOR_SESSION not in record.allowed_decisions:
        return None
    return SessionGrant(
        grant_id=SessionGrantId(f"grant-{uuid4().hex[:12]}"),
        instance_id=record.instance_id,
        runner=record.runner,
        approval_kind=record.kind,
        normalized_scope=scope,
        created_from_approval_id=record.approval_id,
        created_at=datetime.now(UTC),
        workspace_id=record.workspace_id,
    )


def matching_grant(
    record: ApprovalRecord, grants: Sequence[SessionGrant]
) -> SessionGrant | None:
    """The grant that already answers this request, if any.

    Every clause is a way of saying "the user was looking at this exact thing
    when they allowed it". The last one is easy to miss and is not optional: a
    provider that is not offering a session grant *for this request* has no way
    to be told about one, so answering it with a grant would be inventing a
    protocol message rather than replaying a decision.
    """
    if ApprovalDecision.ACCEPT_FOR_SESSION not in record.allowed_decisions:
        return None
    scope = normalized_scope(record)
    if scope is None:
        return None
    for grant in grants:
        if (
            grant.is_active
            and grant.instance_id == record.instance_id
            and grant.runner == record.runner
            and grant.workspace_id == record.workspace_id
            and grant.approval_kind is record.kind
            and grant.normalized_scope == scope
        ):
            return grant
    return None


def _collapsed(text: str | None) -> str:
    """One line, single-spaced. A reflowed command is the same command."""
    return " ".join(text.split()) if text else ""


def _directory(path: str | None) -> str:
    """A directory as one spelling of itself. Unknown stays unknown, and an
    unknown directory only ever matches another unknown one."""
    return os.path.normpath(path) if path else ""


def _paths_in(record: ApprovalRecord) -> tuple[str, ...]:
    """Every file this change touches, absolute where we can tell, sorted.

    Sorted because a set of files is not an order, and two requests that touch
    the same files are the same request whichever order the provider listed
    them in.
    """
    arguments = _parsed(record.arguments)
    if not isinstance(arguments, dict):
        return ()
    found: set[str] = set()
    for field in PATH_FIELDS:
        value = arguments.get(field)
        if isinstance(value, str) and value.strip():
            found.add(_absolute(value, record.cwd))
    for field in PATH_COLLECTION_FIELDS:
        found.update(
            _absolute(path, record.cwd) for path in _collected_paths(arguments.get(field))
        )
    return tuple(sorted(found))


def _collected_paths(value: Any) -> tuple[str, ...]:
    """Paths out of the several shapes providers use to hold more than one."""
    if isinstance(value, dict):
        # A map of path to what is being done to it: the keys are the paths.
        return tuple(key for key in value if isinstance(key, str) and key.strip())
    if not isinstance(value, list):
        return ()
    paths: list[str] = []
    for entry in value:
        if isinstance(entry, str) and entry.strip():
            paths.append(entry)
        elif isinstance(entry, dict):
            paths.extend(
                str(entry[field])
                for field in PATH_FIELDS
                if isinstance(entry.get(field), str) and entry[field].strip()
            )
    return tuple(paths)


def _absolute(path: str, cwd: str | None) -> str:
    """One spelling of one file. A relative path is only meaningful with the
    directory it was relative to, so without one it stays as it came."""
    if os.path.isabs(path) or not cwd:
        return os.path.normpath(path)
    return os.path.normpath(os.path.join(cwd, path))


def _canonical_arguments(arguments: str | None) -> str:
    """Arguments as one comparable string.

    Re-serialized with sorted keys when it is JSON, so two providers -- or two
    releases of one -- that order the same object differently still compare
    equal. Anything unparseable is compared as the text it is: narrower than
    the parsed form, never wider.
    """
    parsed = _parsed(arguments)
    if parsed is None:
        return _collapsed(arguments)
    return json.dumps(parsed, sort_keys=True, separators=(",", ":"))


def _parsed(arguments: str | None) -> Any:
    if not arguments:
        return None
    try:
        return json.loads(arguments)
    except json.JSONDecodeError:
        return None


__all__ = [
    "PATH_COLLECTION_FIELDS",
    "PATH_FIELDS",
    "matching_grant",
    "normalized_scope",
    "session_grant_from",
]
