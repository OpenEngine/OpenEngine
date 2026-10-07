"""Validation definitions shared by the YAML parser and spec generator."""

API_VERSION = "openengine.dev/v1"
KIND = "Graph"
SECTIONS = {"plan": "plan", "implementation": "implement", "review": "review"}
OUTPUT_TYPES = ("string", "number", "integer", "boolean", "list", "object", "findings")
RUNNER_POLICIES = ("least-utilized", "round-robin")
MIN_INTERVAL_SECONDS = 60
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}

FIELDS = {
    "graph": {
        "apiVersion", "kind", "name", "description", "inputs", "repository",
        *SECTIONS, "flow", "loop",
    },
    "input": {"description", "required", "default", "choices"},
    "checkout": {"base_ref", "ref", "name", "description"},
    "human step": {"human", "name", "description"},
    "human": {"prompt", "choose"},
    "ci step": {"ci", "name", "description"},
    "agent step": {
        "agent", "prompt", "name", "description", "tools", "outputs", "model", "steering", "facets",
    },
    "runner": {"not", "same", "choices"},
    "output": {"type", "enum", "required", "lineage", "description"},
    "flow": {"from", "to", "route"},
    "route": {"when", "to"},
    "loop": {"every", "instruction"},
}
