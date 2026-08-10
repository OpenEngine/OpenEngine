"""Composition root for the control server.

Note what is *not* here: any `from engine.adapters...` import. This app is
shipped to consumers, so welding it to a vendor would make the provider-neutral
layers underneath it decorative. The agent backend is resolved by name from
installed plugins instead -- see `engine.runtime.registry`.

    ENGINE_AGENT_RUNNER=anthropic           # exactly this backend, fail if unusable
    ENGINE_AGENT_RUNNER=anthropic,scripted  # first that works (the default)
    ENGINE_AGENT_RUNNER=strands             # a backend we have never heard of

The last line is the point. Installing `engine-adapter-strands` is enough; no
edit here, no fork.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from engine.runtime import resolve_agent_runner

#: Try a live backend, fall back to the offline demo. Deliberately data, not an
#: `if` statement -- a consumer overrides it with one environment variable.
DEFAULT_RUNNER_PREFERENCE: tuple[str, ...] = ("anthropic", "scripted")


@dataclass(frozen=True, slots=True)
class Settings:
    """Everything the composition root needs from the environment."""

    host: str = "127.0.0.1"
    port: int = 8000
    workspace_root: Path = Path(".engine-workspace")
    runner_preference: tuple[str, ...] = field(default=DEFAULT_RUNNER_PREFERENCE)
    model: str | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        preference = os.environ.get("ENGINE_AGENT_RUNNER", "")
        return cls(
            host=os.environ.get("ENGINE_HOST", "127.0.0.1"),
            port=int(os.environ.get("ENGINE_PORT", "8000")),
            workspace_root=Path(
                os.environ.get("ENGINE_WORKSPACE", ".engine-workspace")
            ).resolve(),
            runner_preference=(
                tuple(p.strip() for p in preference.split(",") if p.strip())
                or DEFAULT_RUNNER_PREFERENCE
            ),
            model=os.environ.get("ENGINE_MODEL") or None,
        )


def build_agent_runner(settings: Settings) -> tuple[Any, str]:
    """Resolve the agent backend. Returns (runner, the name that won)."""
    options: dict[str, Any] = {}
    if settings.model:
        options["model"] = settings.model
    return resolve_agent_runner(settings.runner_preference, **options)


__all__ = ["DEFAULT_RUNNER_PREFERENCE", "Settings", "build_agent_runner"]
