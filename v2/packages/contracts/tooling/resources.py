"""Validates OpenEngine resources and writes each service's checked-in HTML reference.

The resources people write (Connection, Graph, Loop, Tool, ToolGrant, ...) are
defined once, as JSON Schemas written in YAML under each service's
`{service}/v1/resources/`, sharing `common/v1/meta.schema.yaml`. The same
files validate a resource (`validate_resource`) and generate that service's
`resources/reference.html`, so the reference cannot drift from what is
enforced. Beyond draft-07 they use one keyword, `x-oe-seconds: {minimum,
maximum}`, bounding a duration such as `1h30m`.

A test regenerates the references and fails when a checked-in page is stale.
Run from `v2/`:

    uv run python packages/contracts/tooling/resources.py               # regenerate the references
    uv run python packages/contracts/tooling/resources.py check FILE..  # validate resource files
"""

from __future__ import annotations

import html
import re
import sys
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft7Validator, FormatChecker, ValidationError, validators
from jsonschema.exceptions import best_match
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT7

from validate import CONTRACTS, _load
from validate import _retrieve as _retrieve_file

META = CONTRACTS / "common" / "v1" / "meta.schema.yaml"
API_VERSION: str = _load(META)["$defs"]["api_version"]["const"]
"""The one apiVersion every resource kind uses, defined in `meta.schema.yaml`."""


@dataclass(frozen=True)
class Group:
    """One service's resources: the kinds it owns."""

    service: str
    title: str
    kinds: tuple[str, ...]

    @property
    def directory(self) -> Path:
        return CONTRACTS / self.service / "v1" / "resources"

    @property
    def examples(self) -> Path:
        return self.directory / "examples"


GROUPS = (
    Group("connection_bus", "Connection Bus", ("Connection",)),
    Group("graph_engine", "Graph Engine", ("Graph", "Loop")),
    Group("tool_service", "Tool Service", ("Tool", "ToolGrant", "ToolInvocation")),
    Group("virtual_control_plane", "Virtual Control Plane", ("Workspace",)),
)
KINDS = tuple(kind for group in GROUPS for kind in group.kinds)
_DURATION_UNITS = {"d": 86400, "h": 3600, "m": 60, "s": 1}


class _Loader(yaml.SafeLoader):
    """YAML as Kubernetes reads it: an unquoted timestamp stays a string."""


_Loader.yaml_implicit_resolvers = {
    first: [(tag, pattern) for tag, pattern in resolvers if tag != "tag:yaml.org,2002:timestamp"]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def load_documents(text: str) -> list[Any]:
    """Every document in a file; resources may share one, separated by `---`."""
    return [document for document in yaml.load_all(text, Loader=_Loader) if document is not None]


def duration_seconds(value: str) -> int:
    return sum(int(amount) * _DURATION_UNITS[unit] for amount, unit in re.findall(r"(\d+)([dhms])", value))


def _seconds(validator: Any, bounds: Mapping[str, int], instance: Any, schema: Any) -> Iterator[ValidationError]:
    if not isinstance(instance, str):
        return
    seconds = duration_seconds(instance)
    if seconds < bounds.get("minimum", 0) or seconds > bounds.get("maximum", seconds):
        yield ValidationError(f"{instance!r} must be from {_duration(bounds['minimum'])} to {_duration(bounds['maximum'])}")


def _duration(seconds: int) -> str:
    unit, size = next((unit, size) for unit, size in _DURATION_UNITS.items() if seconds % size == 0)
    return f"{seconds // size}{unit}"


_ResourceValidator = validators.extend(Draft7Validator, {"x-oe-seconds": _seconds})


def group_of(kind: str) -> Group:
    return next(group for group in GROUPS if kind in group.kinds)


def schema_path(kind: str) -> Path:
    return group_of(kind).directory / f"{kind.lower()}.schema.yaml"


def _retrieve(uri: str) -> Resource:
    """A schema without its `$schema`, which would switch a `$ref` back to plain draft-07 and drop `x-oe-seconds`."""
    contents = {key: value for key, value in _retrieve_file(uri).contents.items() if key != "$schema"}
    return Resource.from_contents(contents, default_specification=DRAFT7)


@cache
def _validator(kind: str) -> Any:
    registry = Registry(retrieve=_retrieve)  # type: ignore[call-arg]
    return _ResourceValidator({"$ref": schema_path(kind).as_uri()}, registry=registry, format_checker=FormatChecker())


@dataclass(frozen=True)
class Problem:
    path: str
    """Where the problem is, such as `spec.plan[0].agent.prompt`; empty for the whole document."""
    message: str

    def __str__(self) -> str:
        return f"{self.path or '(document)'}: {self.message}"


def _path(parts: Iterator[str | int]) -> str:
    text = ""
    for part in parts:
        text += f"[{part}]" if isinstance(part, int) else f".{part}" if text else part
    return text


def _message(error: ValidationError) -> str:
    """A union's error names its members instead of every way each one failed."""
    if error.validator in {"oneOf", "anyOf"} and isinstance(error.instance, Mapping):
        members = [branch["required"][0] for branch in error.validator_value if branch.get("required")]
        present = [member for member in members if member in error.instance]
        exactly = "exactly" if error.validator == "oneOf" else "at least"
        if len(members) == len(error.validator_value) and (not present or (exactly == "exactly" and len(present) > 1)):
            return f"set {exactly} one of {', '.join(members)}"
        # With one member set, its own branch's errors say what is wrong.
        branch = members.index(present[0]) if len(present) == 1 and len(members) == len(error.validator_value) else None
        context = [e for e in error.context or () if branch is None or e.schema_path[0] == branch]
        if context:
            best = best_match(context)
            if best.validator == "not" and branch is not None and best.validator_value.get("required"):
                return f"{', '.join(best.validator_value['required'])} cannot be set with {present[0]}"
            return _message(best)
    return error.message


def validate_resource(document: Any) -> list[Problem]:
    """Every problem with one resource, each with the path it was found at."""
    if not isinstance(document, Mapping):
        return [Problem("", "a resource is a mapping with apiVersion, kind, metadata and spec")]
    if document.get("apiVersion") != API_VERSION:
        return [Problem("apiVersion", f"must be {API_VERSION}")]
    kind = document.get("kind")
    if kind not in KINDS:
        return [Problem("kind", f"must be one of {', '.join(sorted(KINDS))}")]
    errors = sorted(_validator(kind).iter_errors(document), key=lambda error: list(map(str, error.absolute_path)))
    return [Problem(_path(iter(error.absolute_path)), _message(error)) for error in errors]


def validate_text(text: str) -> list[Problem]:
    """Every problem with every resource in a file. With several, paths start with the document's index."""
    try:
        documents = load_documents(text)
    except yaml.YAMLError as error:
        return [Problem("", f"not YAML: {error}")]
    if not documents:
        return [Problem("", "no resources")]
    if len(documents) == 1:
        return validate_resource(documents[0])
    return [
        Problem(f"[{index}]" + (f".{problem.path}" if problem.path else ""), problem.message)
        for index, document in enumerate(documents)
        for problem in validate_resource(document)
    ]


# The reference page.


@dataclass(frozen=True)
class _Type:
    """A titled object schema: one table on the page."""

    title: str
    schema: Mapping[str, Any]
    base: Path


def _resolve(ref: str, base: Path) -> tuple[Mapping[str, Any], Path]:
    file, _, pointer = ref.partition("#")
    path = (base.parent / file).resolve() if file else base
    node: Any = _load(path)
    for part in filter(None, pointer.split("/")):
        node = node[part]
    return node, path


def _anchor(title: str) -> str:
    return title.lower()


def _inline(text: str) -> str:
    """Escaped text with `code` spans."""
    return re.sub(r"`([^`]+)`", r"<code>\1</code>", html.escape(text, quote=False))


def _literal(value: Any) -> str:
    return f"<code>{html.escape(yaml.safe_dump(value, default_flow_style=True, width=1000).strip().removesuffix('...').strip())}</code>"


@dataclass
class _Field:
    type: str = ""
    description: str = ""
    constraints: tuple[str, ...] = ()
    links: tuple[_Type, ...] = ()


def _field(schema: Mapping[str, Any], base: Path) -> _Field:
    """How one property reads: its type, description and constraints, following references."""
    description = schema.get("description", "")
    constraints: list[str] = []
    if "default" in schema:
        constraints.append(f"Default {_literal(schema['default'])}.")
    if "$ref" in schema:
        target, path = _resolve(schema["$ref"], base)
        if "title" in target:
            kind = _Type(target["title"], target, path)
            link = f'<a href="#{_anchor(kind.title)}">{html.escape(kind.title)}</a>'
            return _Field(link, description or target.get("description", ""), tuple(constraints), (kind,))
        inner = _field(target, path)
        return _Field(inner.type, description or inner.description, (*inner.constraints, *constraints), inner.links)
    if "allOf" in schema:
        parts = [_field(member, base) for member in schema["allOf"]]
        return _Field(
            next((part.type for part in parts if part.type), ""),
            description or next((part.description for part in parts if part.description), ""),
            (*(c for part in parts for c in part.constraints), *constraints),
            tuple(link for part in parts for link in part.links),
        )
    kind_name = schema.get("type", "")
    links: tuple[_Type, ...] = ()
    if kind_name == "array":
        item = _field(schema["items"], base)
        kind_name, links = f"[]{item.type}", item.links
        constraints[:0] = [f"Items: {c}" for c in item.constraints if not c.startswith("Default")]
    elif kind_name == "object" and isinstance(schema.get("additionalProperties"), Mapping):
        value = _field(schema["additionalProperties"], base)
        kind_name, links = f"map[string]{value.type}", value.links
        if "propertyNames" in schema:
            keys = _field(schema["propertyNames"], base)
            constraints.extend(f"Keys: {c}" for c in keys.constraints)
        constraints.extend(f"Values: {c}" for c in value.constraints if not c.startswith("Default"))
    if "const" in schema:
        constraints.append(f"Must be {_literal(schema['const'])}.")
    if "enum" in schema:
        constraints.append("One of " + ", ".join(_literal(value) for value in schema["enum"]) + ".")
    if "pattern" in schema:
        constraints.append(f"Pattern {_literal(schema['pattern'])}.")
    if "format" in schema:
        constraints.append(f"Format {html.escape(schema['format'])}.")
    if schema.get("minLength") == 1:
        constraints.append("Not empty.")
    for key, label in (("minLength", "At least {} characters."), ("maxLength", "At most {} characters."),
                       ("minimum", "At least {}."), ("maximum", "At most {}."),
                       ("minItems", "At least {} items."), ("maxItems", "At most {} items."),
                       ("maxProperties", "At most {} entries.")):
        if key in schema and not (key == "minLength" and schema[key] == 1):
            constraints.append(label.format(schema[key]))
    if schema.get("uniqueItems"):
        constraints.append("Unique.")
    if "x-oe-seconds" in schema:
        bounds = schema["x-oe-seconds"]
        constraints.append(f"From {_literal(_duration(bounds['minimum']))} to {_literal(_duration(bounds['maximum']))}.")
    return _Field(kind_name, description, tuple(constraints), links)


def _union(schema: Mapping[str, Any]) -> str:
    for keyword, word in (("oneOf", "exactly"), ("anyOf", "at least")):
        members = [branch["required"][0] for branch in schema.get(keyword, ()) if branch.get("required")]
        if members:
            return f"<p class=\"union\">Set {word} one of {', '.join(f'<code>{m}</code>' for m in members)}.</p>"
    return ""


def _table(kind: _Type) -> tuple[str, list[_Type]]:
    required = set(kind.schema.get("required", ()))
    rows, links = [], []
    for name, schema in (kind.schema.get("properties") or {}).items():
        field = _field(schema, kind.base)
        links.extend(field.links)
        badge = '<span class="req">required</span>' if name in required else ""
        notes = f'<div class="notes">{" ".join(field.constraints)}</div>' if field.constraints else ""
        rows.append(
            f"<tr><td><code class=\"name\">{html.escape(name)}</code>{badge}</td>"
            f"<td class=\"type\">{field.type}</td>"
            f"<td>{_inline(field.description)}{notes}</td></tr>"
        )
    body = (
        "<table><thead><tr><th>Field</th><th>Type</th><th>Description</th></tr></thead><tbody>"
        + "".join(rows) + "</tbody></table>"
        if rows else '<p class="empty">No fields.</p>'
    )
    return body, links


def _examples(kind: str) -> list[tuple[str, str]]:
    """(file name, text) of each example document of `kind`, as written."""
    found = []
    for path in sorted(group_of(kind).examples.glob("*.yaml")):
        for chunk in re.split(r"(?m)^---\s*$", path.read_text()):
            text = re.sub(r"\A(\s*#.*\n)+", "", chunk.strip("\n") + "\n")
            documents = load_documents(text)
            if documents and documents[0].get("kind") == kind:
                found.append((path.name, text))
    return found


_STYLE = """
:root { --bg: #ffffff; --fg: #1c2330; --muted: #5b6577; --line: #dfe3ea; --soft: #f4f6fa;
        --code: #eef2f8; --accent: #2457c5; --req: #a4321c; }
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) { --bg: #12161d; --fg: #e3e7ee; --muted: #98a2b3; --line: #2a313c;
        --soft: #181d26; --code: #222936; --accent: #7aa5ff; --req: #ff9177; }
}
:root[data-theme="dark"] { --bg: #12161d; --fg: #e3e7ee; --muted: #98a2b3; --line: #2a313c;
        --soft: #181d26; --code: #222936; --accent: #7aa5ff; --req: #ff9177; }
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--fg); font: 15px/1.55 system-ui, sans-serif; }
a { color: var(--accent); text-decoration: none; } a:hover { text-decoration: underline; }
code, pre { font: 13px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace; }
code { background: var(--code); padding: 1px 5px; border-radius: 4px; overflow-wrap: anywhere; }
.layout { display: grid; grid-template-columns: 220px minmax(0, 1fr); max-width: 1180px; margin: 0 auto; }
nav { position: sticky; top: 0; align-self: start; max-height: 100vh; overflow-y: auto;
      padding: 24px 16px; border-right: 1px solid var(--line); font-size: 14px; }
nav h2 { font-size: 12px; text-transform: uppercase; letter-spacing: .06em; color: var(--muted); margin: 18px 0 6px; }
nav ul { list-style: none; margin: 0; padding: 0; } nav li { margin: 2px 0; }
nav .kind { font-weight: 600; } nav .sub { padding-left: 12px; }
main { padding: 24px 32px 64px; min-width: 0; }
header p { color: var(--muted); max-width: 72ch; }
h1 { font-size: 26px; margin: 0 0 4px; }
h2.kind { font-size: 22px; margin: 48px 0 4px; padding-top: 12px; border-top: 1px solid var(--line); }
h3 { font-size: 17px; margin: 32px 0 4px; }
.path { color: var(--muted); font-size: 13px; }
.union { font-size: 14px; }
pre { background: var(--soft); border: 1px solid var(--line); border-radius: 6px; padding: 12px 14px; overflow-x: auto; }
details summary { cursor: pointer; color: var(--muted); font-size: 14px; margin: 12px 0 6px; }
table { width: 100%; border-collapse: collapse; margin: 8px 0; font-size: 14px; }
th, td { text-align: left; vertical-align: top; padding: 8px 10px; border-bottom: 1px solid var(--line); }
th { font-size: 12px; text-transform: uppercase; letter-spacing: .05em; color: var(--muted); }
td:first-child { white-space: nowrap; } .type { white-space: nowrap; font-family: ui-monospace, Menlo, monospace; font-size: 13px; }
code.name { background: none; padding: 0; font-weight: 600; }
.req { display: block; color: var(--req); font-size: 11px; text-transform: uppercase; letter-spacing: .05em; }
.notes { color: var(--muted); font-size: 13px; margin-top: 4px; }
.empty { color: var(--muted); }
@media (max-width: 760px) {
  .layout { display: block; } nav { position: static; max-height: none; border-right: 0; border-bottom: 1px solid var(--line); }
  main { padding: 16px; } table, thead, tbody, tr, td { display: block; } thead { display: none; }
  tr { border-bottom: 1px solid var(--line); padding: 8px 0; } td { border: 0; padding: 2px 0; }
}
"""


def page(group: Group) -> str:
    seen: set[str] = set()
    sections, nav = [], []
    for kind in group.kinds:
        path = schema_path(kind)
        root = _load(path)
        stack = [_Type(kind, root, path)]  # depth first, so each type follows the field that introduces it
        types_html, subs = [], []
        while stack:
            current = stack.pop()
            if current.title in seen:
                continue
            seen.add(current.title)
            table, links = _table(current)
            stack.extend(reversed(links))
            source = current.base.relative_to(CONTRACTS).as_posix()
            if current.title == kind:
                examples = "".join(
                    f"<details{' open' if index == 0 else ''}><summary>Example from <code>{html.escape(name)}</code>"
                    f"</summary><pre>{html.escape(text)}</pre></details>"
                    for index, (name, text) in enumerate(_examples(kind))
                )
                types_html.append(
                    f'<h2 class="kind" id="{_anchor(kind)}">{kind}</h2>'
                    f'<div class="path"><code>apiVersion: {API_VERSION}</code> · <code>kind: {kind}</code> · '
                    f'defined in <a href="{html.escape(path.name)}"><code>{html.escape(source)}</code></a></div>'
                    f"<p>{_inline(root['description'])}</p>{examples}{table}"
                )
            else:
                subs.append(current.title)
                types_html.append(
                    f'<h3 id="{_anchor(current.title)}">{html.escape(current.title)}</h3>'
                    f"<p>{_inline(current.schema.get('description', ''))}</p>{_union(current.schema)}{table}"
                )
        sections.append("".join(types_html))
        nav.append(
            f'<li class="kind"><a href="#{_anchor(kind)}">{kind}</a></li>'
            + "".join(f'<li class="sub"><a href="#{_anchor(title)}">{html.escape(title)}</a></li>' for title in subs)
        )
    meta = _load(META)
    title = html.escape(f"{group.title} resources")
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<!-- Generated by packages/contracts/tooling/resources.py from {group.service}/v1/resources/*.schema.yaml. Do not edit. -->
<style>{_STYLE}</style>
</head>
<body>
<div class="layout">
<nav><h2>Resources</h2><ul>{"".join(nav)}</ul></nav>
<main>
<header>
<h1>{title}</h1>
<p>{_inline(meta["description"])}</p>
<p>Validate files with <code>uv run python packages/contracts/tooling/resources.py check FILE…</code> from <code>v2/</code>.</p>
</header>
{"".join(sections)}
</main>
</div>
</body>
</html>
"""


def reference_path(group: Group) -> Path:
    return group.directory / "reference.html"


def main(arguments: list[str]) -> int:
    if arguments[:1] == ["check"]:
        failed = False
        for name in arguments[1:]:
            for problem in validate_text(Path(name).read_text()):
                failed = True
                print(f"{name}: {problem}")
        return int(failed)
    for group in GROUPS:
        reference_path(group).write_text(page(group))
        print(f"wrote {reference_path(group).relative_to(CONTRACTS)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
