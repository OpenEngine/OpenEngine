"""Build and check request envelopes against the contracts.

Commands follow `common/v1/command.schema.json` and must end their causal
path with their own `command_id`; queries follow `common/v1/query.schema.json`.
A service may also check the whole request against its own contract schema,
named like `graph_engine/v1/schemas/queries.schema.json#/$defs/list_graphs_query`.
"""

from __future__ import annotations

import json
import uuid
from functools import cache
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft7Validator, FormatChecker
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT7

from openengine.messaging.errors import ServiceError, invalid_argument

# A link to v2/packages/contracts, so the schemas are read where they are kept.
CONTRACTS = Path(__file__).resolve().parent / "contracts"
COMMAND = "common/v1/command.schema.json"
QUERY = "common/v1/query.schema.json"
QUERY_RESPONSE = "common/v1/query-response.schema.json"
COMMAND_RESPONSE = "common/v1/command-response.schema.json"
MAX_CAUSAL_PATH = 32


class ForeignReference(LookupError):
    """A schema referenced something outside the contracts tree."""

    def __init__(self, uri: str) -> None:
        super().__init__(f"only contract schemas may be referenced, not {uri}")


def _retrieve(uri: str) -> Resource:
    path = Path(uri.removeprefix("file://"))
    if not uri.startswith("file://") or not path.resolve().is_relative_to(CONTRACTS.resolve()):
        raise ForeignReference(uri)
    # Resource schemas, such as a Graph's, are YAML; YAML also reads every JSON schema.
    return Resource.from_contents(yaml.safe_load(path.read_text()), default_specification=DRAFT7)


@cache
def _validator(reference: str) -> Draft7Validator:
    file, _, pointer = reference.partition("#")
    uri = (CONTRACTS / file).resolve().as_uri() + (f"#{pointer}" if pointer else "")
    return Draft7Validator({"$ref": uri}, registry=Registry(retrieve=_retrieve), format_checker=FormatChecker())


def violations(reference: str, body: Any) -> list[tuple[str, str]]:
    """Every way `body` breaks the schema, as (JSON pointer, description)."""
    return [
        ("/" + "/".join(map(str, error.absolute_path)), error.message)
        for error in sorted(_validator(reference).iter_errors(body), key=lambda error: list(map(str, error.path)))
    ]


def check(reference: str, body: Any) -> None:
    """Raise INVALID_ARGUMENT unless `body` conforms."""
    if found := violations(reference, body) + _causal_violations(reference, body):
        raise invalid_argument(found)


def _causal_violations(reference: str, body: Any) -> list[tuple[str, str]]:
    if reference != COMMAND or not isinstance(body, dict):
        return []
    path, command_id = body.get("causal_path"), body.get("command_id")
    if isinstance(path, list) and path and path[-1] != command_id:
        return [("/causal_path", f"must end with the command_id {command_id!r}")]
    return []


def decode(data: bytes) -> Any:
    try:
        return json.loads(data)
    except ValueError as error:
        raise invalid_argument([("/", f"not JSON: {error}")]) from None


def encode(body: dict[str, Any]) -> bytes:
    return json.dumps(body, separators=(",", ":")).encode()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def query(params: dict[str, Any] | None = None, *, request_id: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"request_id": request_id or new_id("req"), "params": params or {}}
    check(QUERY, body)
    return body


def command(
    payload: dict[str, Any],
    *,
    command_id: str | None = None,
    caused_by: list[str] | None = None,
    expected_version: int | None = None,
    deadline_at: str | None = None,
) -> dict[str, Any]:
    """A command whose causal path is its cause's path with its own ID appended."""
    command_id = command_id or new_id("cmd")
    path = [*(caused_by or []), command_id]
    if len(path) > MAX_CAUSAL_PATH:
        raise ServiceError(
            "CAUSAL_PATH_TOO_LONG",
            f"a causal path of {len(path)} elements exceeds {MAX_CAUSAL_PATH}",
            details={"max_length": MAX_CAUSAL_PATH, "length": len(path)},
        )
    body: dict[str, Any] = {"command_id": command_id, "causal_path": path, "payload": payload}
    if expected_version is not None:
        body["expected_version"] = expected_version
    if deadline_at is not None:
        body["deadline_at"] = deadline_at
    check(COMMAND, body)
    return body


@cache
def _response_validator(reference: str) -> Draft7Validator:
    """Validate known fields while accepting additive response evolution."""
    def retrieve(uri: str) -> Resource:
        resource = _retrieve(uri)
        body = resource.contents

        def tolerant(value: Any) -> None:
            if isinstance(value, dict):
                value.pop("additionalProperties", None)
                # The response's retryable flag is authoritative, even when
                # a known code has a different default in our schema release.
                if isinstance(properties := value.get("properties"), dict):
                    if isinstance(retryable := properties.get("retryable"), dict):
                        retryable.pop("const", None)
                for child in value.values():
                    tolerant(child)
            elif isinstance(value, list):
                for child in value:
                    tolerant(child)

        tolerant(body)
        if uri.endswith("/error.schema.json"):
            body["$defs"]["code"].pop("enum", None)
        return Resource.from_contents(body, default_specification=DRAFT7)

    uri = (CONTRACTS / reference).resolve().as_uri()
    return Draft7Validator({"$ref": uri}, registry=Registry(retrieve=retrieve), format_checker=FormatChecker())


def check_response(reference: str, body: Any) -> None:
    if errors := list(_response_validator(reference).iter_errors(body)):
        raise ServiceError("INTERNAL", "invalid response envelope: " + "; ".join(
            "/" + "/".join(map(str, error.absolute_path)) + ": " + error.message for error in errors
        ))
