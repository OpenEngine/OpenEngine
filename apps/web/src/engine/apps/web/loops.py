"""The settings every loop runs under, as the rail's Loops section edits them.

A loop is a standing prompt that idle runners pick up again and again (see
`docs/plans/ongoing-projects.md`). What is kept here are the exit criteria each
loop is held to on its own (when it may work, how many of its pull requests may
be open together, how much it may spend in a day), any one of which stops it,
and the runner strategy shared by all of them.
"""

import json
import math
import re
from collections.abc import Collection, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path

from platformdirs import user_config_path

from engine.apps.web.settings_file import atomic_write_json
from engine.graph_runtime.inputs import LEAST_UTILIZED, ROUND_ROBIN

MANUAL = "manual"
#: `round-robin` here also keeps a run's reviewer off its implementer's runner.
RUNNER_STRATEGIES = (LEAST_UTILIZED, ROUND_ROBIN, MANUAL)

_TIME = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


@dataclass(frozen=True, slots=True)
class LoopSettings:
    """Equal start and end hours mean a loop may start at any time of day."""

    active_hours_start: str = "00:00"
    active_hours_end: str = "00:00"
    max_prs: int = 3
    """Pull requests one loop may have open at once."""
    max_daily_spend: float = 0.0
    """Dollars one loop may spend in a day; zero is no limit."""
    runner_strategy: str = LEAST_UTILIZED
    implementation_runner: str = ""
    """The implementer group's runner, chosen by hand under `manual`."""
    review_runner: str = ""
    """The reviewer group's runner, chosen by hand under `manual`."""

    def json(self) -> dict[str, object]:
        return {
            "activeHours": {"start": self.active_hours_start, "end": self.active_hours_end},
            "maxPrs": self.max_prs,
            "maxDailySpend": self.max_daily_spend,
            "runnerStrategy": self.runner_strategy,
            "implementationRunner": self.implementation_runner,
            "reviewRunner": self.review_runner,
        }


def parse_loop_settings(body: object, runners: Collection[str]) -> LoopSettings:
    """Read the rail's form, refusing anything a loop could not run under."""
    if not isinstance(body, Mapping):
        raise ValueError("loop settings must be an object")
    hours = body.get("activeHours")
    if not isinstance(hours, Mapping):
        raise ValueError("activeHours must be an object")
    start, end = hours.get("start"), hours.get("end")
    if not all(isinstance(value, str) and _TIME.match(value) for value in (start, end)):
        raise ValueError("active hours must be HH:MM")
    max_prs = body.get("maxPrs")
    if isinstance(max_prs, bool) or not isinstance(max_prs, int) or max_prs < 1:
        raise ValueError("maxPrs must be a whole number of at least 1")
    spend = body.get("maxDailySpend")
    if (
        isinstance(spend, bool) or not isinstance(spend, (int, float))
        or not math.isfinite(spend) or spend < 0
    ):
        raise ValueError("maxDailySpend must be a number of at least 0")
    strategy = body.get("runnerStrategy")
    if strategy not in RUNNER_STRATEGIES:
        raise ValueError(f"runnerStrategy must be one of {', '.join(RUNNER_STRATEGIES)}")
    implementation = review = ""
    if strategy == MANUAL:
        implementation = body.get("implementationRunner")
        review = body.get("reviewRunner")
        if implementation not in runners or review not in runners:
            raise ValueError("manual strategy needs an implementation and a review runner")
    return LoopSettings(
        active_hours_start=str(start), active_hours_end=str(end),
        max_prs=max_prs, max_daily_spend=float(spend),
        runner_strategy=str(strategy),
        implementation_runner=str(implementation), review_runner=str(review),
    )


class LoopSettingsStore:
    """A settings file of its own, replaced whole so a stop mid-write leaves
    the previous settings rather than half of the new ones."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or user_config_path("openengine") / "loops.json"

    def get(self) -> LoopSettings:
        try:
            value = json.loads(self._path.read_text(encoding="utf-8"))
            return LoopSettings(**value)
        except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError):
            return LoopSettings()

    def set(self, settings: LoopSettings) -> None:
        atomic_write_json(self._path, asdict(settings))
