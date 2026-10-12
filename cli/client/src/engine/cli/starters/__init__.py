"""The one bundled graph, plus starter aliases retained for one release.

Read source as a resource: the HTTP client never imports the graph runtime.
"""
from importlib.resources import files

DEFAULT = "implementation-review-rerank"
NAMES = (DEFAULT,)
ALIASES = {
    "review": {"review_only": "true"},
    "adversarial-review": {"review_only": "true", "adversarial": "true"},
    "implement-review": {},
    "spec-implement-review": {"spec": "true"},
}


def source(name: str = DEFAULT, *, runner: str = "") -> str:
    if name not in (*NAMES, *ALIASES):
        raise KeyError(f"no built-in graph named {name!r}")
    text = files("engine.cli.starters").joinpath("default_graph.py.txt").read_text(encoding="utf-8")
    if runner:
        # Keep deployment rebuilds through graph_for consistent with onboarding.
        text = text.replace('DEFAULT_RUNNER = "codex"', f"DEFAULT_RUNNER = {runner!r}")
        text = text.replace('DEFAULT_REVIEW_RUNNER = ""', f"DEFAULT_REVIEW_RUNNER = {runner!r}")
    return text
