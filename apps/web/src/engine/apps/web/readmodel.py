"""What the interface displays, and where it comes from.

The pages render from these types and nothing else. They are a *read model*: a
flat, display-shaped projection of a run, deliberately not `engine.domain.RunState`.
The engine's state holds what it needs to decide; a person needs a different cut
of the same run, and coupling a page layout to the decision kernel would turn
every UI tweak into a domain change.

`ReadModel` is the seam the wiring will arrive through. Today there are two
implementations, neither of which touches the world:

* `EmptyReadModel` -- the honest one. Nothing is wired, so there is nothing to
  show.
* `DemoReadModel` -- fixed rows, labelled as such wherever they are drawn, so the
  layout can be judged before there is data to put in it.

The third arrives with the state-store ticket and reads through
`engine.ports.StateStore`. No page changes when it does.
"""

from dataclasses import dataclass
from typing import Protocol

from engine.domain import RunId, RunPhase, TaskId

#: Phases a run cannot leave. Mirrors `RunState.is_terminal`, which takes a
#: state object this layer deliberately does not hold.
TERMINAL_PHASES = frozenset({RunPhase.SUCCEEDED, RunPhase.FAILED})


@dataclass(frozen=True, slots=True)
class RunSummary:
    """One row in the runs table, and the detail panel under it."""

    run_id: RunId
    task_id: TaskId
    phase: RunPhase
    repository: str
    prompt: str
    attempts: int = 0
    review_url: str = ""

    @property
    def is_terminal(self) -> bool:
        return self.phase in TERMINAL_PHASES


@dataclass(frozen=True, slots=True)
class Clarification:
    """A question an agent stopped to ask, waiting on a human.

    The reply is not a call back into the agent -- it becomes an event that
    re-enters the workflow, which is why this type carries no handle to anything.
    """

    run_id: RunId
    asked_by: str
    question: str


class ReadModel(Protocol):
    """Everything the interface is allowed to know.

    A `Protocol`, so an implementation satisfies it by shape -- the same rule the
    ports layer follows, for the same reason: the pages should not be able to
    reach past this into a database, a client, or an adapter.
    """

    def runs(self) -> tuple[RunSummary, ...]:
        """Every run worth showing, newest first."""
        ...

    def clarifications(self) -> tuple[Clarification, ...]:
        """Questions waiting on a human."""
        ...


class EmptyReadModel:
    """Nothing to show, because nothing is wired. Implements `ReadModel`."""

    def runs(self) -> tuple[RunSummary, ...]:
        return ()

    def clarifications(self) -> tuple[Clarification, ...]:
        return ()


#: Invented runs, used only behind the sidebar's demo toggle. Chosen to cover
#: the states the pages have to render differently: mid-flight, published,
#: retried, and failed.
DEMO_RUNS: tuple[RunSummary, ...] = (
    RunSummary(
        run_id=RunId("run-4f2a"),
        task_id=TaskId("ENG-42"),
        phase=RunPhase.ATTEMPTING,
        repository="acme/api",
        prompt="fix the flaky auth test",
        attempts=1,
    ),
    RunSummary(
        run_id=RunId("run-91cd"),
        task_id=TaskId("ENG-41"),
        phase=RunPhase.PUBLISHING,
        repository="acme/api",
        prompt="drop the unused session cache",
        attempts=2,
        review_url="https://github.com/acme/api/pull/1204",
    ),
    RunSummary(
        run_id=RunId("run-0b73"),
        task_id=TaskId("ENG-39"),
        phase=RunPhase.SUCCEEDED,
        repository="acme/web",
        prompt="upgrade the date picker",
        attempts=1,
        review_url="https://github.com/acme/web/pull/877",
    ),
    RunSummary(
        run_id=RunId("run-2e18"),
        task_id=TaskId("ENG-37"),
        phase=RunPhase.FAILED,
        repository="acme/api",
        prompt="migrate the billing schema",
        attempts=3,
    ),
)

DEMO_CLARIFICATIONS: tuple[Clarification, ...] = (
    Clarification(
        run_id=RunId("run-4f2a"),
        asked_by="coder",
        question="The test fails only under the parallel runner. Should I fix the "
        "test's shared fixture, or serialise that module?",
    ),
)


class DemoReadModel:
    """Fixed rows so the layout is reviewable. Implements `ReadModel`."""

    def runs(self) -> tuple[RunSummary, ...]:
        return DEMO_RUNS

    def clarifications(self) -> tuple[Clarification, ...]:
        return DEMO_CLARIFICATIONS


__all__ = [
    "Clarification",
    "DemoReadModel",
    "EmptyReadModel",
    "ReadModel",
    "RunSummary",
    "TERMINAL_PHASES",
]
