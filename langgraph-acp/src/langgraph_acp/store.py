"""What a node's logical identity resolves to, and nothing beyond that.

The one fact a store holds:

    (thread_id="pr-918", session_key="reviewer")  ->  "sess_abc123"

That is the entire record. It is emphatically *not* conversation history, agent
messages, tool history, model context, or agent memory: the ACP agent owns every
one of those and restores them itself when asked to load `sess_abc123`. A store
that grew a transcript would be reimplementing the thing resuming a session
exists to avoid, and would then hold a second version of the conversation that
could disagree with the agent's.

Identity is the pair rather than the thread alone, because one LangGraph thread
routinely runs several agents:

    (pr-918, implementer)  ->  sess_a
    (pr-918, reviewer)     ->  sess_b
    (pr-918, security)     ->  sess_c

and none of them may resume another's conversation.

What the mapping buys is a reply arriving long after the turn that provoked it.
An ACP reviewer leaves a GitHub comment; a webhook carries a reply back days
later; the workflow knows the thread and the session key; the store turns those
into a session id; the agent hydrates its own history. Nothing in that path
reconstructs a transcript -- which is also why a real deployment's store has to
outlive the process, and why `InMemoryACPSessionStore` is explicit about being
the implementation that does not.

The durable implementations -- LangGraph-backed, SQLite, Postgres -- arrive with
their own ticket and change nothing about this interface. The LangGraph one is
deliberately absent rather than merely unwritten: this distribution declares no
dependencies, and importing `langgraph.store` would break the invariant that
`tests/test_public_api.py` enforces. It belongs with the durable stores, beside
the checkpointer it would share a backend with.
"""

from collections.abc import Mapping
from typing import Protocol, runtime_checkable


@runtime_checkable
class ACPSessionStore(Protocol):
    """The persistence boundary between LangGraph identity and an ACP session id.

    Three methods, and deliberately no more. The store's whole contents are the
    two strings that name a conversation and the opaque id they resolve to; a
    dedicated binding type would add a name without adding a fact.

    Every method is async because the implementations worth having talk to a
    database. The in-memory one awaits nothing and is async anyway, so that
    swapping it for a durable store is a constructor argument rather than an
    edit to every caller.
    """

    async def get(self, thread_id: str, session_key: str) -> str | None:
        """The ACP session bound to this identity, or `None` if none is.

        `None` is the ordinary answer on a first invocation rather than a
        failure: it is what tells the `reuse` strategy to create a session and
        bind whatever the agent names it.
        """
        ...

    async def put(self, thread_id: str, session_key: str, acp_session_id: str) -> None:
        """Bind this identity to `acp_session_id`, replacing any earlier binding.

        Last write wins, and the id displaced is not closed or otherwise ended
        -- rebinding is how a node that started a fresh session records it, and
        the previous conversation remains the agent's to keep.
        """
        ...

    async def delete(self, thread_id: str, session_key: str) -> None:
        """Forget this binding. Deleting one that is absent is not an error.

        Deleting does not end the agent's conversation: ACP has no method that
        would, and the id stays resumable by anyone who kept it. What is
        discarded is this package's pointer to it, which is why session close
        and binding removal are separate steps in the ticket that adds both.
        """
        ...


class InMemoryACPSessionStore:
    """A store that lives exactly as long as the process does.

    The right store for tests, for examples, and for a graph whose entire run
    happens inside one process. It is the wrong one the moment a binding has to
    survive a restart: a reviewer that posted a GitHub comment before the worker
    died leaves behind a conversation the agent still holds, and afterwards this
    store can no longer say which one it was.

    The key is the pair itself, not a string built from it. Joining them --
    `f"{thread_id}:{session_key}"` -- would make `("pr-918:security", "review")`
    and `("pr-918", "security:review")` the same binding, and the one guarantee
    this layer owes its caller is that no logical agent resumes another's
    session.

    No lock guards the mapping. Each method is a single `dict` operation, which
    asyncio does not interleave, and an `asyncio.Lock` is bound to the loop that
    created it -- so one here would constrain nothing while quietly refusing to
    be shared between loops.
    """

    def __init__(self, bindings: Mapping[tuple[str, str], str] | None = None) -> None:
        self._bindings: dict[tuple[str, str], str] = dict(bindings or {})

    async def get(self, thread_id: str, session_key: str) -> str | None:
        return self._bindings.get((thread_id, session_key))

    async def put(self, thread_id: str, session_key: str, acp_session_id: str) -> None:
        self._bindings[(thread_id, session_key)] = acp_session_id

    async def delete(self, thread_id: str, session_key: str) -> None:
        self._bindings.pop((thread_id, session_key), None)


__all__ = ["ACPSessionStore", "InMemoryACPSessionStore"]
