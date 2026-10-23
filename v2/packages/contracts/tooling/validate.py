"""Checks the OpenEngine V2 contracts beyond what the AsyncAPI CLI checks.

The AsyncAPI CLI parses each document and resolves its references. This adds
the rules JSON Schema cannot express and the checks only the examples can
prove:

- a message validates against the schema its AsyncAPI message names, and
  its NATS headers against the schema its header trait names;
- a subject matches its channel's address, each `{parameter}` is a single
  token of the identifier type the parameter declares (`x-oe-identifier`),
  and an ID that appears in both the subject and the body agrees;
- a causal path ends with the command's or event's own ID;
- an event's `event_type` ends with the tokens its subject has after the last
  resource ID (all of it, or its fact), and its `Nats-Msg-Id` header equals
  its `event_id`;
- a response names a request subject whose operation replies with it;
- every message has an example, and every example validates.

Run it with `uv run python packages/contracts/tooling/validate.py` from `v2/`.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft7Validator, FormatChecker
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT7

CONTRACTS = Path(__file__).resolve().parent.parent
SERVICES = tuple(sorted(path.parent.parent.name for path in CONTRACTS.glob("*/v1/asyncapi.yaml")))
TYPES = CONTRACTS / "common" / "v1" / "types.schema.json"
MAX_CAUSAL_PATH = 32


@dataclass(frozen=True)
class Message:
    """One AsyncAPI message and where it travels."""

    name: str
    schema: str
    """Absolute `file://` reference to the payload schema."""
    address: str | None
    """Channel address template; None for replies, which go to the requester's inbox."""
    parameters: Mapping[str, str] = field(default_factory=dict)
    """Parameter name -> identifier `$defs` name in `types.schema.json`."""
    replies_to: tuple[str, ...] = ()
    """For a reply, the address templates of the requests it answers."""
    headers: str | None = None
    """Absolute `file://` reference to the headers schema its trait names; None for replies."""


@dataclass(frozen=True)
class Contract:
    service: str
    path: Path
    document: Mapping[str, Any]
    messages: Mapping[str, Message]

    @property
    def examples(self) -> Path:
        return self.path.parent / "examples"


def _load(path: Path) -> Any:
    text = path.read_text()
    return yaml.safe_load(text) if path.suffix in {".yaml", ".yml"} else json.loads(text)


class NonLocalReference(LookupError):
    def __init__(self, uri: str) -> None:
        super().__init__(f"only local references are allowed, not {uri}")


def _retrieve(uri: str) -> Resource:
    if not uri.startswith("file://"):
        raise NonLocalReference(uri)
    return Resource.from_contents(_load(Path(uri.removeprefix("file://"))), default_specification=DRAFT7)


@cache
def _registry() -> Registry:
    return Registry(retrieve=_retrieve)  # type: ignore[call-arg]


def _validator(reference: str) -> Draft7Validator:
    return Draft7Validator({"$ref": reference}, registry=_registry(), format_checker=FormatChecker())


def _resolve_local(document: Mapping[str, Any], ref: str) -> Any:
    node: Any = document
    for part in ref.removeprefix("#/").split("/"):
        node = node[part.replace("~1", "/").replace("~0", "~")]
    return node


def _deref(document: Mapping[str, Any], node: Any) -> Any:
    while isinstance(node, Mapping) and "$ref" in node and str(node["$ref"]).startswith("#/"):
        node = _resolve_local(document, node["$ref"])
    return node


def load_contract(service: str) -> Contract:
    path = CONTRACTS / service / "v1" / "asyncapi.yaml"
    document = _load(path)
    base = path.parent

    def absolute(ref: str) -> str:
        file, _, pointer = ref.partition("#")
        return (base / file).resolve().as_uri() + (f"#{pointer}" if pointer else "")

    def headers_of(message: Mapping[str, Any]) -> str | None:
        for trait in message.get("traits") or ():
            if headers := _deref(document, trait).get("headers"):
                return absolute(headers["schema"]["$ref"])
        return None

    channel_of: dict[str, tuple[str | None, dict[str, str]]] = {}
    for channel in document["channels"].values():
        if channel["address"] is None:
            continue  # the reply inbox; replies are typed by their request below
        parameters = {
            name: _deref(document, parameter)["x-oe-identifier"]
            for name, parameter in (channel.get("parameters") or {}).items()
        }
        for reference in (channel.get("messages") or {}).values():
            name = reference["$ref"].rsplit("/", 1)[-1]
            channel_of[name] = (channel["address"], parameters)

    replies: dict[str, list[str]] = {}
    reply_parameters: dict[str, dict[str, str]] = {}
    for operation in document["operations"].values():
        reply = operation.get("reply")
        if not reply:
            continue
        request = _deref(document, operation["channel"])
        for reference in reply["messages"]:
            name = _deref(document, reference)["name"]
            replies.setdefault(name, []).append(request["address"])
            # A reply's subject is the requester's inbox; its request's tokens are typed by the request channel.
            reply_parameters.setdefault(name, {}).update(
                (parameter, _deref(document, value)["x-oe-identifier"])
                for parameter, value in (request.get("parameters") or {}).items()
            )

    messages: dict[str, Message] = {}
    for name, raw in document["components"]["messages"].items():
        address, parameters = channel_of.get(name, (None, reply_parameters.get(name, {})))
        messages[name] = Message(
            name=name,
            schema=absolute(raw["payload"]["schema"]["$ref"]),
            address=address,
            parameters=parameters,
            replies_to=tuple(replies.get(name, ())),
            headers=headers_of(raw),
        )
    return Contract(service, path, document, messages)


def match_subject(template: str, subject: str) -> dict[str, str] | None:
    """The `{parameter}` values of `subject`, or None if it does not fit `template`."""
    expected, actual = template.split("."), subject.split(".")
    if len(expected) != len(actual):
        return None
    values: dict[str, str] = {}
    for want, got in zip(expected, actual, strict=True):
        if want.startswith("{") and want.endswith("}"):
            values[want[1:-1]] = got
        elif want != got:
            return None
    return values


def _literal_tail(template: str) -> list[str]:
    """The tokens after the template's last `{parameter}`."""
    tokens = template.split(".")
    last = max((i for i, token in enumerate(tokens) if token.startswith("{")), default=-1)
    return tokens[last + 1:]


def _subject_errors(message: Message, template: str, subject: str, label: str) -> list[str]:
    values = match_subject(template, subject)
    if values is None:
        return [f"{label} {subject!r} does not match {template!r}"]
    errors = []
    for name, value in values.items():
        identifier = f"{TYPES.as_uri()}#/$defs/{message.parameters.get(name) or name}"
        if not _validator(identifier).is_valid(value):
            errors.append(f"{label} parameter {name}={value!r} is not a valid {message.parameters.get(name, name)}")
    return errors


def _mentions(body: Mapping[str, Any], name: str) -> Iterator[tuple[str, Any]]:
    """Where `name` appears in the parts of a message that identify resources."""
    if name in body:
        yield f"/{name}", body[name]
    for section in ("resources", "payload", "params"):
        part = body.get(section)
        if isinstance(part, Mapping) and name in part:
            yield f"/{section}/{name}", part[name]


def validate_message(
    contract: Contract,
    message_name: str,
    body: Any,
    *,
    subject: str | None = None,
    in_reply_to: str | None = None,
    headers: Mapping[str, str] | None = None,
) -> list[str]:
    """Every way `body` breaks the contract for `message_name`; empty when it conforms."""
    message = contract.messages.get(message_name)
    if message is None:
        return [f"{contract.service} has no message {message_name!r}"]
    errors = [
        f"{'/'.join(map(str, error.absolute_path)) or '(root)'}: {error.message}"
        for error in _validator(message.schema).iter_errors(body)
    ]
    if headers is not None and message.headers is not None:
        errors += [
            f"headers/{'/'.join(map(str, error.absolute_path)) or '(root)'}: {error.message}"
            for error in _validator(message.headers).iter_errors(headers)
        ]
    if not isinstance(body, Mapping):
        return errors

    path = body.get("causal_path")
    own_id = body.get("event_id", body.get("command_id"))
    if isinstance(path, list) and path and own_id is not None and path[-1] != own_id:
        errors.append(f"causal_path ends with {path[-1]!r}, not the message's own ID {own_id!r}")

    if message.address is not None:
        if subject is None:
            errors.append(f"{message_name} needs the subject it was sent on")
        else:
            errors += _subject_errors(message, message.address, subject, "subject")
            for name, value in (match_subject(message.address, subject) or {}).items():
                for pointer, mentioned in _mentions(body, name):
                    if mentioned != value:
                        errors.append(f"{pointer} is {mentioned!r} but the subject names {value!r}")
            event_type = body.get("event_type")
            tail = ".".join(_literal_tail(message.address))
            if isinstance(event_type, str) and event_type != tail and not event_type.endswith("." + tail):
                errors.append(f"event_type {event_type!r} does not match the subject's {tail!r}")
        if "event_id" in body:
            if (headers or {}).get("Nats-Msg-Id") != body["event_id"]:
                errors.append("the Nats-Msg-Id header must equal event_id")
    elif in_reply_to is None:
        errors.append(f"{message_name} is a reply and needs the request subject it answers")
    else:
        fits = [template for template in message.replies_to if match_subject(template, in_reply_to) is not None]
        if not fits:
            errors.append(f"{message_name} does not answer requests on {in_reply_to!r}")
        else:
            errors += _subject_errors(message, fits[0], in_reply_to, "request subject")
    return errors


def validate_example(contract: Contract, path: Path) -> list[str]:
    example = _load(path)
    return validate_message(
        contract,
        example["message"],
        example["payload"],
        subject=example.get("subject"),
        in_reply_to=example.get("in_reply_to"),
        headers=example.get("headers"),
    )


def check(contract: Contract) -> list[str]:
    """All problems with one service's contract and examples."""
    problems: list[str] = []
    examples = sorted(contract.examples.glob("*.json"))
    covered = set()
    for path in examples:
        covered.add(_load(path)["message"])
        problems += [f"{path.relative_to(CONTRACTS)}: {error}" for error in validate_example(contract, path)]
    for name in sorted(set(contract.messages) - covered):
        problems.append(f"{contract.service}: message {name} has no example")
    for name, operation in contract.document["operations"].items():
        if operation["action"] == "receive" and not operation.get("reply"):
            problems.append(f"{contract.service}: operation {name} receives requests but declares no reply")
    for raw in contract.document["components"]["messages"].values():
        for example in raw.get("examples", []):
            ref = example["payload"]["$ref"].partition("#")[0]
            if not (contract.path.parent / ref).exists():
                problems.append(f"{contract.service}: message {raw['name']} cites missing example {ref}")
    return problems


def main() -> int:
    problems = [problem for service in SERVICES for problem in check(load_contract(service))]
    for problem in problems:
        print(problem, file=sys.stderr)
    print(f"{len(problems)} problem(s) in {len(SERVICES)} contracts")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
