"""Validation definitions shared by the YAML parser and spec generator."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Callable, Mapping


API_VERSION = "openengine.dev/v1"
KIND = "Graph"
SECTIONS = {"plan": "plan", "implementation": "implement", "review": "review"}
OUTPUT_TYPES = ("string", "number", "integer", "boolean", "list", "object", "findings")
RUNNER_POLICIES = ("least-utilized", "round-robin")
MIN_INTERVAL_SECONDS = 60
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}

FIELDS = {
    "graph": {
        "description", "inputs", "repository",
        *SECTIONS, "flow", "loop",
    },
    "input": {"description", "default", "choices"},
    "checkout": {"base_ref", "ref", "name", "description"},
    "human step": {"human", "name", "description"},
    "human": {"prompt", "choose"},
    "ci step": {"ci", "name", "description"},
    "agent step": {
        "agent", "name", "description", "tools", "outputs", "facets",
    },
    "runner": {"not", "same", "choices"},
    "output": {"enum", "lineage", "description"},
    "flow": {"from", "to", "route"},
    "route": {"when", "to"},
    "loop": {"every"},
}


_MISSING = object()


@dataclass(frozen=True)
class Field:
    """A scalar field's contract, consumed by validation and reference rendering.

    Cross-field and graph-wide checks remain in language.py.
    """

    description: str
    types: tuple[type, ...] = ()
    default: Any = _MISSING
    choices: tuple[Any, ...] = ()
    pattern: str | None = None
    nonblank: bool = False
    error: str = "invalid value"
    fallback: Any = ""

    def read(self, raw: Mapping[str, Any], key: str, path: str,
             problem: Callable[[str, str], None]) -> Any:
        value = raw.get(key, self.default)
        valid = value is not _MISSING
        valid = valid and (not self.types or isinstance(value, self.types))
        valid = valid and (not self.choices or value in self.choices)
        valid = valid and (self.pattern is None or isinstance(value, str) and re.match(self.pattern, value))
        valid = valid and (not self.nonblank or isinstance(value, str) and bool(value.strip()))
        if not valid:
            problem(path, self.error)
            return self.fallback
        return value

    def constraints(self) -> str:
        parts = ["required" if self.default is _MISSING else f"default: `{self.default!r}`"]
        if self.types:
            parts.append("type: " + ", ".join(t.__name__ for t in self.types))
        if self.choices:
            parts.append("choices: " + ", ".join(f"`{v!r}`" for v in self.choices))
        if self.pattern:
            parts.append(f"pattern: `{self.pattern}`")
        if self.nonblank:
            parts.append("nonblank")
        return "; ".join(parts)


_REQUIRED = Field("Whether the value is required.", types=(bool,), default=False,
                  error="must be true or false", fallback=False)
FIELD_RULES = {
    "loop": {
        "instruction": Field("Default instruction for recurring runs; null or empty values are normalized to empty text.",
                             types=(str,), default="", error="must be text"),
    },
    "graph": {
        "apiVersion": Field("Graph language version.", choices=(API_VERSION,),
                            error=f"required; must be {API_VERSION}"),
        "kind": Field("Definition kind.", default=KIND, choices=(KIND,), error=f"must be {KIND}"),
        "name": Field("Project-scoped graph name.", types=(str,),
                      pattern=r"^[a-z0-9][a-z0-9._-]{0,62}$",
                      error="required; lowercase letters, digits, '.', '_' and '-'"),
    },
    "input": {"required": _REQUIRED},
    "agent step": {
        "prompt": Field("What the agent is asked to do.", types=(str,), nonblank=True,
                        error="required; what the agent is asked to do"),
        "model": Field("Model tier, model name, or expression template.", types=(str,), default="",
                       error="must be a tier such as default or elevated, a model, or ${...}"),
        "steering": Field("Allow steering while the agent runs.", default=None,
                          choices=(None, "always-open"), error="must be always-open", fallback=None),
    },
    "output": {
        "required": _REQUIRED,
        "type": Field("Output value type.", default="string", choices=OUTPUT_TYPES,
                      error=f"must be one of {', '.join(OUTPUT_TYPES)}", fallback="string"),
    },
}

# Scalar declarations also determine which keys are admitted.
for declaration, rules in FIELD_RULES.items():
    FIELDS[declaration].update(rules)
