"""The decision function.

`decide` is the whole point of the architecture: a synchronous, side-effect-free
function from `(state, event)` to `(state, commands)`. It performs no I/O, takes
no clock, and holds no references to adapters -- so it can be tested by calling
it, and replayed deterministically by a workflow runtime.

Anything that needs the outside world comes back as a `Command` for the runtime
to dispatch. If you ever feel the urge to `import` an adapter here, the thing you
actually want is a new command.

Ticket 1 ships the signature and one representative branch; the full state
machine follows in a later ticket.
"""

from engine.domain.commands import Command, ProvisionWorkspace
from engine.domain.events import Event, RunRequested
from engine.domain.state import RunPhase, RunState


class Decision(tuple[RunState, tuple[Command, ...]]):
    """Result of `decide`: the next state plus commands to dispatch.

    A tuple subclass so it unpacks naturally (`state, commands = decide(...)`)
    while still supporting `.state` / `.commands` at call sites that read better
    that way.
    """

    __slots__ = ()

    def __new__(cls, state: RunState, commands: tuple[Command, ...]) -> "Decision":
        return super().__new__(cls, (state, commands))

    @property
    def state(self) -> RunState:
        return self[0]

    @property
    def commands(self) -> tuple[Command, ...]:
        return self[1]


def decide(state: RunState, event: Event) -> Decision:
    """Fold one event into the run, returning the next state and any commands.

    Total by construction: an unrecognised event is a no-op rather than an
    error, so an adapter emitting something new can never wedge a live run.
    """
    if state.is_terminal:
        return Decision(state, ())

    match event:
        case RunRequested(run_id=run_id, task_id=task_id, prompt=prompt, repository=repository):
            next_state = RunState(
                run_id=run_id,
                task_id=task_id,
                phase=RunPhase.PREPARING_WORKSPACE,
                repository=repository,
                prompt=prompt,
            )
            return Decision(
                next_state,
                (ProvisionWorkspace(run_id=run_id, repository=repository, base_ref="main"),),
            )
        case _:
            # Remaining transitions arrive with the engine ticket.
            return Decision(state, ())


__all__ = ["Decision", "decide"]
