"""Built-in default graph and one-release starter aliases."""
from importlib.resources import files

DEFAULT = "implementation-review-rerank"
VERBS = dict.fromkeys(("go", "review", "fix"), DEFAULT)
NAMES = (DEFAULT,)
ALIASES = {
    "review": (DEFAULT, {"review_only": "true"}),
    "adversarial-review": (DEFAULT, {"review_only": "true", "adversarial": "true"}),
    "implement-review": (DEFAULT, {}),
    "spec-implement-review": (DEFAULT, {"spec": "true"}),
}


def source(name: str = DEFAULT, *, runner: str = "") -> str:
    name = ALIASES.get(name, (name, {}))[0]
    if name not in NAMES:
        raise KeyError(f"no built-in graph named {name!r}")
    text = files("engine.cli.starters").joinpath("default_graph.py.txt").read_text(encoding="utf-8")
    if runner:
        text = text.replace('DEFAULT_REVIEW_RUNNER = ""', f"DEFAULT_REVIEW_RUNNER = {runner!r}")
    return text.replace('DEFAULT_RUNNER = "codex"', f"DEFAULT_RUNNER = {runner!r}") if runner else text
