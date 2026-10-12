"""Built-in verb graphs and one-release starter aliases."""
from importlib.resources import files

DEFAULT = "implement"
VERBS = {"go": "implement", "review": "review", "fix": "fix"}
NAMES = tuple(VERBS.values())
ALIASES = {
    "review": ("review", {}),
    "adversarial-review": ("review", {"adversarial": "true"}),
    "implement-review": ("implement", {}),
    "spec-implement-review": ("implement", {"spec": "true"}),
}


def source(name: str = DEFAULT, *, runner: str = "") -> str:
    name = ALIASES.get(name, (name, {}))[0]
    if name not in NAMES:
        raise KeyError(f"no built-in graph named {name!r}")
    text = files("engine.cli.starters").joinpath(f"{name}.py.txt").read_text(encoding="utf-8")
    return text.replace('DEFAULT_RUNNER = "codex"', f"DEFAULT_RUNNER = {runner!r}") if runner else text
